"""Facilities to analyze entire programs, including imported modules.

Parse and analyze the source files of a program in the correct order
(based on file dependencies), and collect the results.

This module only directs a build, which is performed in multiple passes per
file.  The individual passes are implemented in separate modules.

The function build() is the main interface to this module.
"""
# TODO: More consistent terminology, e.g. path/fnam, module/id, state/file

import binascii
import contextlib
import gc
import hashlib
import json
import os
import re
import stat
import sys
import time
import errno
import types

from typing import (AbstractSet, Any, Dict, Iterable, Iterator, List,
                    Mapping, NamedTuple, Optional, Set, Tuple, Union, Callable)
MYPY = False
if MYPY:
    from typing import ClassVar
    from typing_extensions import Final

from mypy.nodes import (MypyFile, ImportBase, Import, ImportFrom, ImportAll)
from mypy.semanal_pass1 import SemanticAnalyzerPass1
from mypy.semanal import SemanticAnalyzerPass2, apply_semantic_analyzer_patches
from mypy.semanal_pass3 import SemanticAnalyzerPass3
from mypy.checker import TypeChecker
from mypy.indirection import TypeIndirectionVisitor
from mypy.errors import Errors, CompileError, report_internal_error
from mypy.util import DecodeError, decode_python_encoding, is_sub_path
from mypy.report import Reports
from mypy import moduleinfo
from mypy.fixup import fixup_module
from mypy.modulefinder import BuildSource, compute_search_paths, FindModuleCache, SearchPaths
from mypy.nodes import Expression
from mypy.options import Options
from mypy.parse import parse
from mypy.stats import dump_type_stats
from mypy.types import Type
from mypy.version import __version__
from mypy.plugin import Plugin, ChainedPlugin, plugin_types
from mypy.plugins.default import DefaultPlugin
from mypy.server.deps import get_dependencies
from mypy.fscache import FileSystemCache
from mypy.metastore import MetadataStore, FilesystemMetadataStore, SqliteMetadataStore
from mypy.typestate import TypeState, reset_global_state

from mypy.mypyc_hacks import BuildManagerBase


# Switch to True to produce debug output related to fine-grained incremental
# mode only that is useful during development. This produces only a subset of
# output compared to --verbose output. We use a global flag to enable this so
# that it's easy to enable this when running tests.
DEBUG_FINE_GRAINED = False  # type: Final


Graph = Dict[str, 'State']


# TODO: Get rid of BuildResult.  We might as well return a BuildManager.
class BuildResult:
    """The result of a successful build.

    Attributes:
      manager: The build manager.
      files:   Dictionary from module name to related AST node.
      types:   Dictionary from parse tree node to its inferred type.
      used_cache: Whether the build took advantage of a pre-existing cache
      errors:  List of error messages.
    """

    def __init__(self, manager: 'BuildManager', graph: Graph) -> None:
        self.manager = manager
        self.graph = graph
        self.files = manager.modules
        self.types = manager.all_types  # Non-empty if export_types True in options
        self.used_cache = manager.cache_enabled
        self.errors = []  # type: List[str]  # Filled in by build if desired


class BuildSourceSet:
    """Efficiently test a file's membership in the set of build sources."""

    def __init__(self, sources: List[BuildSource]) -> None:
        self.source_text_present = False
        self.source_modules = set()  # type: Set[str]
        self.source_paths = set()  # type: Set[str]

        for source in sources:
            if source.text is not None:
                self.source_text_present = True
            elif source.path:
                self.source_paths.add(source.path)
            else:
                self.source_modules.add(source.module)

    def is_source(self, file: MypyFile) -> bool:
        if file.path and file.path in self.source_paths:
            return True
        elif file._fullname in self.source_modules:
            return True
        elif file.path is None and self.source_text_present:
            return True
        else:
            return False


def build(sources: List[BuildSource],
          options: Options,
          alt_lib_path: Optional[str] = None,
          flush_errors: Optional[Callable[[List[str], bool], None]] = None,
          fscache: Optional[FileSystemCache] = None,
          ) -> BuildResult:
    """Analyze a program.

    A single call to build performs parsing, semantic analysis and optionally
    type checking for the program *and* all imported modules, recursively.

    Return BuildResult if successful or only non-blocking errors were found;
    otherwise raise CompileError.

    If a flush_errors callback is provided, all error messages will be
    passed to it and the errors and messages fields of BuildResult and
    CompileError (respectively) will be empty. Otherwise those fields will
    report any error messages.

    Args:
      sources: list of sources to build
      options: build options
      alt_lib_path: an additional directory for looking up library modules
        (takes precedence over other directories)
      flush_errors: optional function to flush errors after a file is processed
      fscache: optionally a file-system cacher

    """
    # If we were not given a flush_errors, we use one that will populate those
    # fields for callers that want the traditional API.
    messages = []

    def default_flush_errors(new_messages: List[str], is_serious: bool) -> None:
        messages.extend(new_messages)

    flush_errors = flush_errors or default_flush_errors

    try:
        result = _build(sources, options, alt_lib_path, flush_errors, fscache)
        result.errors = messages
        return result
    except CompileError as e:
        # CompileErrors raised from an errors object carry all of the
        # messages that have not been reported out by error streaming.
        # Patch it up to contain either none or all none of the messages,
        # depending on whether we are flushing errors.
        serious = not e.use_stdout
        flush_errors(e.messages, serious)
        e.messages = messages
        raise


def _build(sources: List[BuildSource],
           options: Options,
           alt_lib_path: Optional[str],
           flush_errors: Callable[[List[str], bool], None],
           fscache: Optional[FileSystemCache],
           ) -> BuildResult:
    # This seems the most reasonable place to tune garbage collection.
    gc.set_threshold(150 * 1000)

    data_dir = default_data_dir()
    fscache = fscache or FileSystemCache()

    search_paths = compute_search_paths(sources, options, data_dir, alt_lib_path)

    reports = Reports(data_dir, options.report_dirs)
    source_set = BuildSourceSet(sources)
    errors = Errors(options.show_error_context, options.show_column_numbers)
    plugin, snapshot = load_plugins(options, errors)

    # Construct a build manager object to hold state during the build.
    #
    # Ignore current directory prefix in error messages.
    manager = BuildManager(data_dir, search_paths,
                           ignore_prefix=os.getcwd(),
                           source_set=source_set,
                           reports=reports,
                           options=options,
                           version_id=__version__,
                           plugin=plugin,
                           plugins_snapshot=snapshot,
                           errors=errors,
                           flush_errors=flush_errors,
                           fscache=fscache)

    reset_global_state()
    try:
        graph = dispatch(sources, manager)
        if not options.fine_grained_incremental:
            TypeState.reset_all_subtype_caches()
        return BuildResult(manager, graph)
    finally:
        manager.metastore.commit()
        manager.log("Build finished in %.3f seconds with %d modules, and %d errors" %
                    (time.time() - manager.start_time,
                     len(manager.modules),
                     manager.errors.num_messages()))
        # Finish the HTML or XML reports even if CompileError was raised.
        reports.finish()


def default_data_dir() -> str:
    """Returns directory containing typeshed directory."""
    return os.path.dirname(__file__)


CacheMeta = NamedTuple('CacheMeta',
                       [('id', str),
                        ('path', str),
                        ('mtime', int),
                        ('size', int),
                        ('hash', str),
                        ('dependencies', List[str]),  # names of imported modules
                        ('data_mtime', int),  # mtime of data_json
                        ('deps_mtime', Optional[int]),  # mtime of deps_json
                        ('data_json', str),  # path of <id>.data.json
                        # path of <id>.deps.json, which we use to store fine-grained
                        # dependency information for fine-grained mode
                        ('deps_json', Optional[str]),
                        ('suppressed', List[str]),  # dependencies that weren't imported
                        ('child_modules', List[str]),  # all submodules of the given module
                        ('options', Optional[Dict[str, object]]),  # build options
                        # dep_prios and dep_lines are in parallel with
                        # dependencies + suppressed.
                        ('dep_prios', List[int]),
                        ('dep_lines', List[int]),
                        ('interface_hash', str),  # hash representing the public interface
                        ('version_id', str),  # mypy version for cache invalidation
                        ('ignore_all', bool),  # if errors were ignored
                        ])
# NOTE: dependencies + suppressed == all reachable imports;
# suppressed contains those reachable imports that were prevented by
# silent mode or simply not found.


def cache_meta_from_dict(meta: Dict[str, Any],
                         data_json: str, deps_json: Optional[str]) -> CacheMeta:
    """Build a CacheMeta object from a json metadata dictionary

    Args:
      meta: JSON metadata read from the metadata cache file
      data_json: Path to the .data.json file containing the AST trees
      deps_json: Optionally, path to the .deps.json file containing
                 fine-grained dependency information.
    """
    sentinel = None  # type: Any  # Values to be validated by the caller
    return CacheMeta(
        meta.get('id', sentinel),
        meta.get('path', sentinel),
        int(meta['mtime']) if 'mtime' in meta else sentinel,
        meta.get('size', sentinel),
        meta.get('hash', sentinel),
        meta.get('dependencies', []),
        int(meta['data_mtime']) if 'data_mtime' in meta else sentinel,
        int(meta['deps_mtime']) if meta.get('deps_mtime') is not None else None,
        data_json,
        deps_json,
        meta.get('suppressed', []),
        meta.get('child_modules', []),
        meta.get('options'),
        meta.get('dep_prios', []),
        meta.get('dep_lines', []),
        meta.get('interface_hash', ''),
        meta.get('version_id', sentinel),
        meta.get('ignore_all', True),
    )


# Priorities used for imports.  (Here, top-level includes inside a class.)
# These are used to determine a more predictable order in which the
# nodes in an import cycle are processed.
PRI_HIGH = 5  # type: Final  # top-level "from X import blah"
PRI_MED = 10  # type: Final  # top-level "import X"
PRI_LOW = 20  # type: Final  # either form inside a function
PRI_MYPY = 25  # type: Final  # inside "if MYPY" or "if TYPE_CHECKING"
PRI_INDIRECT = 30  # type: Final  # an indirect dependency
PRI_ALL = 99  # type: Final  # include all priorities


def import_priority(imp: ImportBase, toplevel_priority: int) -> int:
    """Compute import priority from an import node."""
    if not imp.is_top_level:
        # Inside a function
        return PRI_LOW
    if imp.is_mypy_only:
        # Inside "if MYPY" or "if typing.TYPE_CHECKING"
        return max(PRI_MYPY, toplevel_priority)
    # A regular import; priority determined by argument.
    return toplevel_priority


def load_plugins(options: Options, errors: Errors) -> Tuple[Plugin, Dict[str, str]]:
    """Load all configured plugins.

    Return a plugin that encapsulates all plugins chained together. Always
    at least include the default plugin (it's last in the chain).
    The second return value is a snapshot of versions/hashes of loaded user
    plugins (for cache validation).
    """
    import importlib
    snapshot = {}  # type: Dict[str, str]

    default_plugin = DefaultPlugin(options)  # type: Plugin
    if not options.config_file:
        return default_plugin, snapshot

    line = find_config_file_line_number(options.config_file, 'mypy', 'plugins')
    if line == -1:
        line = 1  # We need to pick some line number that doesn't look too confusing

    def plugin_error(message: str) -> None:
        errors.report(line, 0, message)
        errors.raise_error()

    custom_plugins = []  # type: List[Plugin]
    errors.set_file(options.config_file, None)
    for plugin_path in options.plugins:
        func_name = 'plugin'
        plugin_dir = None  # type: Optional[str]
        if ':' in os.path.basename(plugin_path):
            plugin_path, func_name = plugin_path.rsplit(':', 1)
        if plugin_path.endswith('.py'):
            # Plugin paths can be relative to the config file location.
            plugin_path = os.path.join(os.path.dirname(options.config_file), plugin_path)
            if not os.path.isfile(plugin_path):
                plugin_error("Can't find plugin '{}'".format(plugin_path))
            # Use an absolute path to avoid populating the cache entry
            # for 'tmp' during tests, since it will be different in
            # different tests.
            plugin_dir = os.path.abspath(os.path.dirname(plugin_path))
            fnam = os.path.basename(plugin_path)
            module_name = fnam[:-3]
            sys.path.insert(0, plugin_dir)
        elif re.search(r'[\\/]', plugin_path):
            fnam = os.path.basename(plugin_path)
            plugin_error("Plugin '{}' does not have a .py extension".format(fnam))
        else:
            module_name = plugin_path

        try:
            module = importlib.import_module(module_name)
        except Exception:
            plugin_error("Error importing plugin '{}'".format(plugin_path))
        finally:
            if plugin_dir is not None:
                assert sys.path[0] == plugin_dir
                del sys.path[0]

        if not hasattr(module, func_name):
            plugin_error('Plugin \'{}\' does not define entry point function "{}"'.format(
                plugin_path, func_name))

        try:
            plugin_type = getattr(module, func_name)(__version__)
        except Exception:
            print('Error calling the plugin(version) entry point of {}\n'.format(plugin_path))
            raise  # Propagate to display traceback

        if not isinstance(plugin_type, type):
            plugin_error(
                'Type object expected as the return value of "plugin"; got {!r} (in {})'.format(
                    plugin_type, plugin_path))
        if not issubclass(plugin_type, plugin_types):
            plugin_error(
                'Return value of "plugin" must be a subclass of "mypy.plugin.Plugin" '
                '(in {})'.format(plugin_path))
        try:
            custom_plugins.append(plugin_type(options))
            snapshot[module_name] = take_module_snapshot(module)
        except Exception:
            print('Error constructing plugin instance of {}\n'.format(plugin_type.__name__))
            raise  # Propagate to display traceback
    # Custom plugins take precedence over the default plugin.
    return ChainedPlugin(options, custom_plugins + [default_plugin]), snapshot


def take_module_snapshot(module: types.ModuleType) -> str:
    """Take plugin module snapshot by recording its version and hash.

    We record _both_ hash and the version to detect more possible changes
    (e.g. if there is a change in modules imported by a plugin).
    """
    if hasattr(module, '__file__'):
        with open(module.__file__, 'rb') as f:
            digest = hashlib.md5(f.read()).hexdigest()
    else:
        digest = 'unknown'
    ver = getattr(module, '__version__', 'none')
    return '{}:{}'.format(ver, digest)


def find_config_file_line_number(path: str, section: str, setting_name: str) -> int:
    """Return the approximate location of setting_name within mypy config file.

    Return -1 if can't determine the line unambiguously.
    """
    in_desired_section = False
    try:
        results = []
        with open(path) as f:
            for i, line in enumerate(f):
                line = line.strip()
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1].strip()
                    in_desired_section = (current_section == section)
                elif in_desired_section and re.match(r'{}\s*='.format(setting_name), line):
                    results.append(i + 1)
        if len(results) == 1:
            return results[0]
    except OSError:
        pass
    return -1


class BuildManager(BuildManagerBase):
    """This class holds shared state for building a mypy program.

    It is used to coordinate parsing, import processing, semantic
    analysis and type checking.  The actual build steps are carried
    out by dispatch().

    Attributes:
      data_dir:        Mypy data directory (contains stubs)
      search_paths:    SearchPaths instance indicating where to look for modules
      modules:         Mapping of module ID to MypyFile (shared by the passes)
      semantic_analyzer:
                       Semantic analyzer, pass 2
      semantic_analyzer_pass3:
                       Semantic analyzer, pass 3
      all_types:       Map {Expression: Type} from all modules (enabled by export_types)
      options:         Build options
      missing_modules: Set of modules that could not be imported encountered so far
      stale_modules:   Set of modules that needed to be rechecked (only used by tests)
      version_id:      The current mypy version (based on commit id when possible)
      plugin:          Active mypy plugin(s)
      plugins_snapshot:
                       Snapshot of currently active user plugins (versions and hashes)
      old_plugins_snapshot:
                       Plugins snapshot from previous incremental run (or None in
                       non-incremental mode and if cache was not found)
      errors:          Used for reporting all errors
      flush_errors:    A function for processing errors after each SCC
      cache_enabled:   Whether cache is being read. This is set based on options,
                       but is disabled if fine-grained cache loading fails
                       and after an initial fine-grained load. This doesn't
                       determine whether we write cache files or not.
      stats:           Dict with various instrumentation numbers, it is used
                       not only for debugging, but also required for correctness,
                       in particular to check consistency of the protocol dependency cache.
      fscache:         A file system cacher
    """

    def __init__(self, data_dir: str,
                 search_paths: SearchPaths,
                 ignore_prefix: str,
                 source_set: BuildSourceSet,
                 reports: Reports,
                 options: Options,
                 version_id: str,
                 plugin: Plugin,
                 plugins_snapshot: Dict[str, str],
                 errors: Errors,
                 flush_errors: Callable[[List[str], bool], None],
                 fscache: FileSystemCache,
                 ) -> None:
        super().__init__()
        self.start_time = time.time()
        self.data_dir = data_dir
        self.errors = errors
        self.errors.set_ignore_prefix(ignore_prefix)
        self.search_paths = search_paths
        self.source_set = source_set
        self.reports = reports
        self.options = options
        self.version_id = version_id
        self.modules = {}  # type: Dict[str, MypyFile]
        self.missing_modules = set()  # type: Set[str]
        self.plugin = plugin
        self.semantic_analyzer = SemanticAnalyzerPass2(self.modules, self.missing_modules,
                                                  self.errors, self.plugin)
        self.semantic_analyzer_pass3 = SemanticAnalyzerPass3(self.modules, self.errors,
                                                             self.semantic_analyzer)
        self.all_types = {}  # type: Dict[Expression, Type]  # Enabled by export_types
        self.indirection_detector = TypeIndirectionVisitor()
        self.stale_modules = set()  # type: Set[str]
        self.rechecked_modules = set()  # type: Set[str]
        self.flush_errors = flush_errors
        self.cache_enabled = options.incremental and (
            not options.fine_grained_incremental or options.use_fine_grained_cache)
        self.fscache = fscache
        self.find_module_cache = FindModuleCache(self.search_paths, self.fscache, self.options)
        if options.sqlite_cache:
            self.metastore = SqliteMetadataStore(_cache_dir_prefix(self))  # type: MetadataStore
        else:
            self.metastore = FilesystemMetadataStore(_cache_dir_prefix(self))

        # a mapping from source files to their corresponding shadow files
        # for efficient lookup
        self.shadow_map = {}  # type: Dict[str, str]
        if self.options.shadow_file is not None:
            self.shadow_map = {source_file: shadow_file
                               for (source_file, shadow_file)
                               in self.options.shadow_file}
        # a mapping from each file being typechecked to its possible shadow file
        self.shadow_equivalence_map = {}  # type: Dict[str, Optional[str]]
        self.plugin = plugin
        self.plugins_snapshot = plugins_snapshot
        self.old_plugins_snapshot = read_plugins_snapshot(self)

    def use_fine_grained_cache(self) -> bool:
        return self.cache_enabled and self.options.use_fine_grained_cache

    def maybe_swap_for_shadow_path(self, path: str) -> str:
        if not self.shadow_map:
            return path

        previously_checked = path in self.shadow_equivalence_map
        if not previously_checked:
            for source, shadow in self.shadow_map.items():
                if self.fscache.samefile(path, source):
                    self.shadow_equivalence_map[path] = shadow
                    break
                else:
                    self.shadow_equivalence_map[path] = None

        shadow_file = self.shadow_equivalence_map.get(path)
        return shadow_file if shadow_file else path

    def get_stat(self, path: str) -> os.stat_result:
        return self.fscache.stat(self.maybe_swap_for_shadow_path(path))

    def getmtime(self, path: str) -> int:
        """Return a file's mtime; but 0 in bazel mode.

        (Bazel's distributed cache doesn't like filesystem metadata to
        end up in output files.)
        """
        if self.options.bazel:
            return 0
        else:
            return int(self.metastore.getmtime(path))

    def normpath(self, path: str) -> str:
        """Convert path to absolute; but to relative in bazel mode.

        (Bazel's distributed cache doesn't like filesystem metadata to
        end up in output files.)
        """
        # TODO: Could we always use relpath?  (A worry in non-bazel
        # mode would be that a moved file may change its full module
        # name without changing its size, mtime or hash.)
        if self.options.bazel:
            return os.path.relpath(path)
        else:
            return os.path.abspath(path)

    def all_imported_modules_in_file(self,
                                     file: MypyFile) -> List[Tuple[int, str, int]]:
        """Find all reachable import statements in a file.

        Return list of tuples (priority, module id, import line number)
        for all modules imported in file; lower numbers == higher priority.

        Can generate blocking errors on bogus relative imports.
        """

        def correct_rel_imp(imp: Union[ImportFrom, ImportAll]) -> str:
            """Function to correct for relative imports."""
            file_id = file.fullname()
            rel = imp.relative
            if rel == 0:
                return imp.id
            if os.path.basename(file.path).startswith('__init__.'):
                rel -= 1
            if rel != 0:
                file_id = ".".join(file_id.split(".")[:-rel])
            new_id = file_id + "." + imp.id if imp.id else file_id

            if not new_id:
                self.errors.set_file(file.path, file.name())
                self.errors.report(imp.line, 0,
                                   "No parent module -- cannot perform relative import",
                                   blocker=True)

            return new_id

        res = []  # type: List[Tuple[int, str, int]]
        for imp in file.imports:
            if not imp.is_unreachable:
                if isinstance(imp, Import):
                    pri = import_priority(imp, PRI_MED)
                    ancestor_pri = import_priority(imp, PRI_LOW)
                    for id, _ in imp.ids:
                        # We append the target (e.g. foo.bar.baz)
                        # before the ancestors (e.g. foo and foo.bar)
                        # so that, if FindModuleCache finds the target
                        # module in a package marked with py.typed
                        # underneath a namespace package installed in
                        # site-packages, (gasp), that cache's
                        # knowledge of the ancestors can be primed
                        # when it is asked to find the target.
                        res.append((pri, id, imp.line))
                        ancestor_parts = id.split(".")[:-1]
                        ancestors = []
                        for part in ancestor_parts:
                            ancestors.append(part)
                            res.append((ancestor_pri, ".".join(ancestors), imp.line))
                elif isinstance(imp, ImportFrom):
                    cur_id = correct_rel_imp(imp)
                    pos = len(res)
                    all_are_submodules = True
                    # Also add any imported names that are submodules.
                    pri = import_priority(imp, PRI_MED)
                    for name, __ in imp.names:
                        sub_id = cur_id + '.' + name
                        if self.is_module(sub_id):
                            res.append((pri, sub_id, imp.line))
                        else:
                            all_are_submodules = False
                    # Add cur_id as a dependency, even if all of the
                    # imports are submodules. Processing import from will try
                    # to look through cur_id, so we should depend on it.
                    # As a workaround for for some bugs in cycle handling (#4498),
                    # if all of the imports are submodules, do the import at a lower
                    # priority.
                    pri = import_priority(imp, PRI_HIGH if not all_are_submodules else PRI_LOW)
                    res.insert(pos, ((pri, cur_id, imp.line)))
                elif isinstance(imp, ImportAll):
                    pri = import_priority(imp, PRI_HIGH)
                    res.append((pri, correct_rel_imp(imp), imp.line))

        return res

    def is_module(self, id: str) -> bool:
        """Is there a file in the file system corresponding to module id?"""
        return self.find_module_cache.find_module(id) is not None

    def parse_file(self, id: str, path: str, source: str, ignore_errors: bool) -> MypyFile:
        """Parse the source of a file with the given name.

        Raise CompileError if there is a parse error.
        """
        num_errs = self.errors.num_messages()
        tree = parse(source, path, id, self.errors, options=self.options)
        tree._fullname = id
        self.add_stats(files_parsed=1,
                       modules_parsed=int(not tree.is_stub),
                       stubs_parsed=int(tree.is_stub))

        if self.errors.num_messages() != num_errs:
            self.log("Bailing due to parse errors")
            self.errors.raise_error()

        self.errors.set_file_ignored_lines(path, tree.ignored_lines, ignore_errors)
        return tree

    def report_file(self,
                    file: MypyFile,
                    type_map: Dict[Expression, Type],
                    options: Options) -> None:
        if self.source_set.is_source(file):
            self.reports.file(file, type_map, options)

    def stats_summary(self) -> Mapping[str, object]:
        return self.stats


def write_protocol_deps_cache(proto_deps: Dict[str, Set[str]],
                              manager: BuildManager, graph: Graph) -> None:
    """Write cache files for protocol dependencies.

    Serialize protocol dependencies map for fine grained mode. Also take the snapshot
    of current sources to later check consistency between protocol cache and individual
    cache files.

    Out of three kinds of protocol dependencies described in TypeState._snapshot_protocol_deps,
    only the last two kinds are stored in global protocol caches, dependencies of the first kind
    (i.e. <SuperProto[wildcard]>, <Proto[wildcard]> -> <Proto>) are written to the normal
    per-file fine grained dependency caches.
    """
    metastore = manager.metastore
    proto_meta, proto_cache = get_protocol_deps_cache_name()
    meta_snapshot = {}  # type: Dict[str, str]
    error = False
    for id, st in graph.items():
        # If we didn't parse a file (so it doesn't have a
        # source_hash), then it must be a module with a fresh cache,
        # so use the hash from that.
        if st.source_hash:
            meta_snapshot[id] = st.source_hash
        else:
            assert st.meta, "Module must be either parsed or cached"
            meta_snapshot[id] = st.meta.hash

    if not metastore.write(proto_meta, json.dumps(meta_snapshot)):
        manager.log("Error writing protocol meta JSON file {}".format(proto_cache))
        error = True
    listed_proto_deps = {k: list(v) for (k, v) in proto_deps.items()}
    if not metastore.write(proto_cache, json.dumps(listed_proto_deps)):
        manager.log("Error writing protocol deps JSON file {}".format(proto_cache))
        error = True
    if error:
        manager.errors.set_file(_cache_dir_prefix(manager), None)
        manager.errors.report(0, 0, "Error writing protocol dependencies cache",
                              blocker=True)


PLUGIN_SNAPSHOT_FILE = '@plugins_snapshot.json'  # type: Final


def write_plugins_snapshot(manager: BuildManager) -> None:
    """Write snapshot of versions and hashes of currently active plugins."""
    if not manager.metastore.write(PLUGIN_SNAPSHOT_FILE, json.dumps(manager.plugins_snapshot)):
        manager.errors.set_file(_cache_dir_prefix(manager), None)
        manager.errors.report(0, 0, "Error writing plugins snapshot",
                              blocker=True)


def read_plugins_snapshot(manager: BuildManager) -> Optional[Dict[str, str]]:
    """Read cached snapshot of versions and hashes of plugins from previous run."""
    snapshot = _load_json_file(PLUGIN_SNAPSHOT_FILE, manager,
                               log_sucess='Plugins snapshot ',
                               log_error='Could not load plugins snapshot: ')
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        manager.log('Could not load plugins snapshot: cache is not a dict: {}'
                    .format(type(snapshot)))
        return None
    return snapshot


def read_protocol_cache(manager: BuildManager,
                        graph: Graph) -> Optional[Dict[str, Set[str]]]:
    """Read and validate protocol dependencies cache.

    See docstring for write_protocol_cache for details about which kinds of
    dependencies are read.
    """
    proto_meta, proto_cache = get_protocol_deps_cache_name()
    meta_snapshot = _load_json_file(proto_meta, manager,
                                    log_sucess='Proto meta ',
                                    log_error='Could not load protocol metadata: ')
    if meta_snapshot is None:
        return None
    # Take a snapshot of the source hashes from all of the metas we found.
    # (Including the ones we rejected because they were out of date.)
    # We use this to verify that they match up with the proto_deps.
    current_meta_snapshot = {id: st.meta_source_hash for id, st in graph.items()
                             if st.meta_source_hash is not None}

    common = set(meta_snapshot.keys()) & set(current_meta_snapshot.keys())
    if any(meta_snapshot[id] != current_meta_snapshot[id] for id in common):
        # TODO: invalidate also if options changed (like --strict-optional)?
        manager.log('Protocol cache inconsistent, ignoring')
        return None
    deps = _load_json_file(proto_cache, manager,
                           log_sucess='Proto deps ',
                           log_error='Could not load protocol cache: ')
    if deps is None:
        return None
    if not isinstance(deps, dict):
        manager.log('Could not load protocol cache: cache is not a dict: {}'
                    .format(type(deps)))
        return None
    return {k: set(v) for (k, v) in deps.items()}


def _load_json_file(file: str, manager: BuildManager,
                    log_sucess: str, log_error: str) -> Optional[Dict[str, Any]]:
    """A simple helper to read a JSON file with logging."""
    try:
        data = manager.metastore.read(file)
    except IOError:
        manager.log(log_error + file)
        return None
    manager.trace(log_sucess + data.rstrip())
    result = json.loads(data)  # TODO: Errors
    return result


def _cache_dir_prefix(manager: BuildManager) -> str:
    """Get current cache directory (or file if id is given)."""
    cache_dir = manager.options.cache_dir
    pyversion = manager.options.python_version
    base = os.path.join(cache_dir, '%d.%d' % pyversion)
    return base


def get_cache_names(id: str, path: str, manager: BuildManager) -> Tuple[str, str, Optional[str]]:
    """Return the file names for the cache files.

    Args:
      id: module ID
      path: module path
      cache_dir: cache directory
      pyversion: Python version (major, minor)

    Returns:
      A tuple with the file names to be used for the meta JSON, the
      data JSON, and the fine-grained deps JSON, respectively.
    """
    pair = manager.options.cache_map.get(path)
    if pair is not None:
        # The cache map paths were specified relative to the base directory,
        # but the filesystem metastore APIs operates relative to the cache
        # prefix directory.
        # Solve this by rewriting the paths as relative to the root dir.
        # This only makes sense when using the filesystem backed cache.
        root = _cache_dir_prefix(manager)
        return (os.path.relpath(pair[0], root), os.path.relpath(pair[1], root), None)
    prefix = os.path.join(*id.split('.'))
    is_package = os.path.basename(path).startswith('__init__.py')
    if is_package:
        prefix = os.path.join(prefix, '__init__')

    deps_json = None
    if manager.options.cache_fine_grained:
        deps_json = prefix + '.deps.json'
    return (prefix + '.meta.json', prefix + '.data.json', deps_json)


def get_protocol_deps_cache_name() -> Tuple[str, str]:
    """Return file names for fine grained protocol dependencies cache.

    Since these dependencies represent a global state of the program, they
    are serialized per program, not per module, and the corresponding files
    live at the root of the cache folder for a given Python version.
    Return a tuple ('meta file path', 'data file path'), where the meta file
    contains hashes of all source files at the time the protocol dependencies
    were written, and data file contains the protocol dependencies.
    """
    name = '@proto_deps'
    return name + '.meta.json', name + '.data.json'


def find_cache_meta(id: str, path: str, manager: BuildManager) -> Optional[CacheMeta]:
    """Find cache data for a module.

    Args:
      id: module ID
      path: module path
      manager: the build manager (for pyversion, log/trace, and build options)

    Returns:
      A CacheMeta instance if the cache data was found and appears
      valid; otherwise None.
    """
    # TODO: May need to take more build options into account
    meta_json, data_json, deps_json = get_cache_names(id, path, manager)
    manager.trace('Looking for {} at {}'.format(id, meta_json))
    meta = _load_json_file(meta_json, manager,
                           log_sucess='Meta {} '.format(id),
                           log_error='Could not load cache for {}: '.format(id))
    if meta is None:
        return None
    if not isinstance(meta, dict):
        manager.log('Could not load cache for {}: meta cache is not a dict: {}'
                    .format(id, repr(meta)))
        return None
    m = cache_meta_from_dict(meta, data_json, deps_json)
    # Don't check for path match, that is dealt with in validate_meta().
    if (m.id != id or
            m.mtime is None or m.size is None or
            m.dependencies is None or m.data_mtime is None or
            (manager.options.cache_fine_grained and m.deps_mtime is None)):
        manager.log('Metadata abandoned for {}: attributes are missing'.format(id))
        return None

    # Ignore cache if generated by an older mypy version.
    if ((m.version_id != manager.version_id and not manager.options.skip_version_check)
            or m.options is None
            or len(m.dependencies) + len(m.suppressed) != len(m.dep_prios)
            or len(m.dependencies) + len(m.suppressed) != len(m.dep_lines)):
        manager.log('Metadata abandoned for {}: new attributes are missing'.format(id))
        return None

    # Ignore cache if (relevant) options aren't the same.
    # Note that it's fine to mutilate cached_options since it's only used here.
    cached_options = m.options
    current_options = manager.options.clone_for_module(id).select_options_affecting_cache()
    if manager.options.quick_and_dirty:
        # In quick_and_dirty mode allow non-quick_and_dirty cache files.
        cached_options['quick_and_dirty'] = True
    if manager.options.skip_version_check:
        # When we're lax about version we're also lax about platform.
        cached_options['platform'] = current_options['platform']
    if 'debug_cache' in cached_options:
        # Older versions included debug_cache, but it's silly to compare it.
        del cached_options['debug_cache']
    if cached_options != current_options:
        manager.log('Metadata abandoned for {}: options differ'.format(id))
        if manager.options.verbosity >= 2:
            for key in sorted(set(cached_options) | set(current_options)):
                if cached_options.get(key) != current_options.get(key):
                    manager.trace('    {}: {} != {}'
                                  .format(key, cached_options.get(key), current_options.get(key)))
        return None
    if manager.old_plugins_snapshot and manager.plugins_snapshot:
        # Check if plugins are still the same.
        if manager.plugins_snapshot != manager.old_plugins_snapshot:
            manager.log('Metadata abandoned for {}: plugins differ'.format(id))
            return None

    manager.add_stats(fresh_metas=1)
    return m


def validate_meta(meta: Optional[CacheMeta], id: str, path: Optional[str],
                  ignore_all: bool, manager: BuildManager) -> Optional[CacheMeta]:
    '''Checks whether the cached AST of this module can be used.

    Returns:
      None, if the cached AST is unusable.
      Original meta, if mtime/size matched.
      Meta with mtime updated to match source file, if hash/size matched but mtime/path didn't.
    '''
    # This requires two steps. The first one is obvious: we check that the module source file
    # contents is the same as it was when the cache data file was created. The second one is not
    # too obvious: we check that the cache data file mtime has not changed; it is needed because
    # we use cache data file mtime to propagate information about changes in the dependencies.

    if meta is None:
        manager.log('Metadata not found for {}'.format(id))
        return None

    if meta.ignore_all and not ignore_all:
        manager.log('Metadata abandoned for {}: errors were previously ignored'.format(id))
        return None

    bazel = manager.options.bazel
    assert path is not None, "Internal error: meta was provided without a path"
    # Check data_json; assume if its mtime matches it's good.
    # TODO: stat() errors
    data_mtime = manager.getmtime(meta.data_json)
    if data_mtime != meta.data_mtime:
        manager.log('Metadata abandoned for {}: data cache is modified'.format(id))
        return None
    deps_mtime = None
    if manager.options.cache_fine_grained:
        assert meta.deps_json
        deps_mtime = manager.getmtime(meta.deps_json)
        if deps_mtime != meta.deps_mtime:
            manager.log('Metadata abandoned for {}: deps cache is modified'.format(id))
            return None

    path = manager.normpath(path)
    try:
        st = manager.get_stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        manager.log('Metadata abandoned for {}: file {} does not exist'.format(id, path))
        return None

    # When we are using a fine-grained cache, we want our initial
    # build() to load all of the cache information and then do a
    # fine-grained incremental update to catch anything that has
    # changed since the cache was generated. We *don't* want to do a
    # coarse-grained incremental rebuild, so we accept the cache
    # metadata even if it doesn't match the source file.
    #
    # We still *do* the mtime/md5 checks, however, to enable
    # fine-grained mode to take advantage of the mtime-updating
    # optimization when mtimes differ but md5s match.  There is
    # essentially no extra time cost to computing the hash here, since
    # it will be cached and will be needed for finding changed files
    # later anyways.
    fine_grained_cache = manager.use_fine_grained_cache()

    size = st.st_size
    # Bazel ensures the cache is valid.
    if size != meta.size and not bazel and not fine_grained_cache:
        manager.log('Metadata abandoned for {}: file {} has different size'.format(id, path))
        return None

    # Bazel ensures the cache is valid.
    mtime = 0 if bazel else int(st.st_mtime)
    if not bazel and (mtime != meta.mtime or path != meta.path):
        try:
            source_hash = manager.fscache.md5(path)
        except (OSError, UnicodeDecodeError, DecodeError):
            return None
        if source_hash != meta.hash:
            if fine_grained_cache:
                manager.log('Using stale metadata for {}: file {}'.format(id, path))
                return meta
            else:
                manager.log('Metadata abandoned for {}: file {} has different hash'.format(
                    id, path))
                return None
        else:
            # Optimization: update mtime and path (otherwise, this mismatch will reappear).
            meta = meta._replace(mtime=mtime, path=path)
            # Construct a dict we can pass to json.dumps() (compare to write_cache()).
            meta_dict = {
                'id': id,
                'path': path,
                'mtime': mtime,
                'size': size,
                'hash': source_hash,
                'data_mtime': data_mtime,
                'deps_mtime': deps_mtime,
                'dependencies': meta.dependencies,
                'suppressed': meta.suppressed,
                'child_modules': meta.child_modules,
                'options': (manager.options.clone_for_module(id)
                            .select_options_affecting_cache()),
                'dep_prios': meta.dep_prios,
                'dep_lines': meta.dep_lines,
                'interface_hash': meta.interface_hash,
                'version_id': manager.version_id,
                'ignore_all': meta.ignore_all,
            }
            if manager.options.debug_cache:
                meta_str = json.dumps(meta_dict, indent=2, sort_keys=True)
            else:
                meta_str = json.dumps(meta_dict)
            meta_json, _, _ = get_cache_names(id, path, manager)
            manager.log('Updating mtime for {}: file {}, meta {}, mtime {}'
                        .format(id, path, meta_json, meta.mtime))
            manager.metastore.write(meta_json, meta_str)  # Ignore errors, just an optimization.
            return meta

    # It's a match on (id, path, size, hash, mtime).
    manager.log('Metadata fresh for {}: file {}'.format(id, path))
    return meta


def compute_hash(text: str) -> str:
    # We use md5 instead of the builtin hash(...) function because the output of hash(...)
    # can differ between runs due to hash randomization (enabled by default in Python 3.3).
    # See the note in https://docs.python.org/3/reference/datamodel.html#object.__hash__.
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def json_dumps(obj: Any, debug_cache: bool) -> str:
    if debug_cache:
        return json.dumps(obj, indent=2, sort_keys=True)
    else:
        return json.dumps(obj, sort_keys=True)


def write_cache(id: str, path: str, tree: MypyFile,
                serialized_fine_grained_deps: Dict[str, List[str]],
                dependencies: List[str], suppressed: List[str],
                child_modules: List[str], dep_prios: List[int], dep_lines: List[int],
                old_interface_hash: str, source_hash: str,
                ignore_all: bool, manager: BuildManager) -> Tuple[str, Optional[CacheMeta]]:
    """Write cache files for a module.

    Note that this mypy's behavior is still correct when any given
    write_cache() call is replaced with a no-op, so error handling
    code that bails without writing anything is okay.

    Args:
      id: module ID
      path: module path
      tree: the fully checked module data
      dependencies: module IDs on which this module depends
      suppressed: module IDs which were suppressed as dependencies
      child_modules: module IDs which are this package's direct submodules
      dep_prios: priorities (parallel array to dependencies)
      dep_lines: import line locations (parallel array to dependencies)
      old_interface_hash: the hash from the previous version of the data cache file
      source_hash: the hash of the source code
      ignore_all: the ignore_all flag for this module
      manager: the build manager (for pyversion, log/trace)

    Returns:
      A tuple containing the interface hash and CacheMeta
      corresponding to the metadata that was written (the latter may
      be None if the cache could not be written).
    """
    metastore = manager.metastore
    # For Bazel we use relative paths and zero mtimes.
    bazel = manager.options.bazel

    # Obtain file paths.
    path = manager.normpath(path)
    meta_json, data_json, deps_json = get_cache_names(id, path, manager)
    manager.log('Writing {} {} {} {} {}'.format(
        id, path, meta_json, data_json, deps_json))

    # Update tree.path so that in bazel mode it's made relative (since
    # sometimes paths leak out).
    if bazel:
        tree.path = path

    # Serialize data and analyze interface
    data = tree.serialize()
    data_str = json_dumps(data, manager.options.debug_cache)
    interface_hash = compute_hash(data_str)

    # Obtain and set up metadata
    try:
        st = manager.get_stat(path)
    except OSError as err:
        manager.log("Cannot get stat for {}: {}".format(path, err))
        # Remove apparently-invalid cache files.
        # (This is purely an optimization.)
        for filename in [data_json, meta_json]:
            try:
                os.remove(filename)
            except OSError:
                pass
        # Still return the interface hash we computed.
        return interface_hash, None

    # Write data cache file, if applicable
    # Note that for Bazel we don't record the data file's mtime.
    if old_interface_hash == interface_hash:
        # If the interface is unchanged, the cached data is guaranteed
        # to be equivalent, and we only need to update the metadata.
        data_mtime = manager.getmtime(data_json)
        manager.trace("Interface for {} is unchanged".format(id))
    else:
        manager.trace("Interface for {} has changed".format(id))
        if not metastore.write(data_json, data_str):
            # Most likely the error is the replace() call
            # (see https://github.com/python/mypy/issues/3215).
            manager.log("Error writing data JSON file {}".format(data_json))
            # Let's continue without writing the meta file.  Analysis:
            # If the replace failed, we've changed nothing except left
            # behind an extraneous temporary file; if the replace
            # worked but the getmtime() call failed, the meta file
            # will be considered invalid on the next run because the
            # data_mtime field won't match the data file's mtime.
            # Both have the effect of slowing down the next run a
            # little bit due to an out-of-date cache file.
            return interface_hash, None
        data_mtime = manager.getmtime(data_json)

    deps_mtime = None
    if deps_json:
        deps_str = json_dumps(serialized_fine_grained_deps, manager.options.debug_cache)
        if not metastore.write(deps_json, deps_str):
            manager.log("Error writing deps JSON file {}".format(deps_json))
            return interface_hash, None
        deps_mtime = manager.getmtime(deps_json)

    mtime = 0 if bazel else int(st.st_mtime)
    size = st.st_size
    options = manager.options.clone_for_module(id)
    assert source_hash is not None
    meta = {'id': id,
            'path': path,
            'mtime': mtime,
            'size': size,
            'hash': source_hash,
            'data_mtime': data_mtime,
            'deps_mtime': deps_mtime,
            'dependencies': dependencies,
            'suppressed': suppressed,
            'child_modules': child_modules,
            'options': options.select_options_affecting_cache(),
            'dep_prios': dep_prios,
            'dep_lines': dep_lines,
            'interface_hash': interface_hash,
            'version_id': manager.version_id,
            'ignore_all': ignore_all,
            }

    # Write meta cache file
    meta_str = json_dumps(meta, manager.options.debug_cache)
    if not metastore.write(meta_json, meta_str):
        # Most likely the error is the replace() call
        # (see https://github.com/python/mypy/issues/3215).
        # The next run will simply find the cache entry out of date.
        manager.log("Error writing meta JSON file {}".format(meta_json))

    return interface_hash, cache_meta_from_dict(meta, data_json, deps_json)


def delete_cache(id: str, path: str, manager: BuildManager) -> None:
    """Delete cache files for a module.

    The cache files for a module are deleted when mypy finds errors there.
    This avoids inconsistent states with cache files from different mypy runs,
    see #4043 for an example.
    """
    path = manager.normpath(path)
    cache_paths = get_cache_names(id, path, manager)
    manager.log('Deleting {} {} {}'.format(id, path, " ".join(x for x in cache_paths if x)))

    for filename in cache_paths:
        try:
            if filename:
                manager.metastore.remove(filename)
        except OSError as e:
            if e.errno != errno.ENOENT:
                manager.log("Error deleting cache file {}: {}".format(filename, e.strerror))


"""Dependency manager.

Design
======

Ideally
-------

A. Collapse cycles (each SCC -- strongly connected component --
   becomes one "supernode").

B. Topologically sort nodes based on dependencies.

C. Process from leaves towards roots.

Wrinkles
--------

a. Need to parse source modules to determine dependencies.

b. Processing order for modules within an SCC.

c. Must order mtimes of files to decide whether to re-process; depends
   on clock never resetting.

d. from P import M; checks filesystem whether module P.M exists in
   filesystem.

e. Race conditions, where somebody modifies a file while we're
   processing. Solved by using a FileSystemCache.


Steps
-----

1. For each explicitly given module find the source file location.

2. For each such module load and check the cache metadata, and decide
   whether it's valid.

3. Now recursively (or iteratively) find dependencies and add those to
   the graph:

   - for cached nodes use the list of dependencies from the cache
     metadata (this will be valid even if we later end up re-parsing
     the same source);

   - for uncached nodes parse the file and process all imports found,
     taking care of (a) above.

Step 3 should also address (d) above.

Once step 3 terminates we have the entire dependency graph, and for
each module we've either loaded the cache metadata or parsed the
source code.  (However, we may still need to parse those modules for
which we have cache metadata but that depend, directly or indirectly,
on at least one module for which the cache metadata is stale.)

Now we can execute steps A-C from the first section.  Finding SCCs for
step A shouldn't be hard; there's a recipe here:
http://code.activestate.com/recipes/578507/.  There's also a plethora
of topsort recipes, e.g. http://code.activestate.com/recipes/577413/.

For single nodes, processing is simple.  If the node was cached, we
deserialize the cache data and fix up cross-references.  Otherwise, we
do semantic analysis followed by type checking.  We also handle (c)
above; if a module has valid cache data *but* any of its
dependencies was processed from source, then the module should be
processed from source.

A relatively simple optimization (outside SCCs) we might do in the
future is as follows: if a node's cache data is valid, but one or more
of its dependencies are out of date so we have to re-parse the node
from source, once we have fully type-checked the node, we can decide
whether its symbol table actually changed compared to the cache data
(by reading the cache data and comparing it to the data we would be
writing).  If there is no change we can declare the node up to date,
and any node that depends (and for which we have cached data, and
whose other dependencies are up to date) on it won't need to be
re-parsed from source.

Import cycles
-------------

Finally we have to decide how to handle (c), import cycles.  Here
we'll need a modified version of the original state machine
(build.py), but we only need to do this per SCC, and we won't have to
deal with changes to the list of nodes while we're processing it.

If all nodes in the SCC have valid cache metadata and all dependencies
outside the SCC are still valid, we can proceed as follows:

  1. Load cache data for all nodes in the SCC.

  2. Fix up cross-references for all nodes in the SCC.

Otherwise, the simplest (but potentially slow) way to proceed is to
invalidate all cache data in the SCC and re-parse all nodes in the SCC
from source.  We can do this as follows:

  1. Parse source for all nodes in the SCC.

  2. Semantic analysis for all nodes in the SCC.

  3. Type check all nodes in the SCC.

(If there are more passes the process is the same -- each pass should
be done for all nodes before starting the next pass for any nodes in
the SCC.)

We could process the nodes in the SCC in any order.  For sentimental
reasons, I've decided to process them in the reverse order in which we
encountered them when originally constructing the graph.  That's how
the old build.py deals with cycles, and at least this reproduces the
previous implementation more accurately.

Can we do better than re-parsing all nodes in the SCC when any of its
dependencies are out of date?  It's doubtful.  The optimization
mentioned at the end of the previous section would require re-parsing
and type-checking a node and then comparing its symbol table to the
cached data; but because the node is part of a cycle we can't
technically type-check it until the semantic analysis of all other
nodes in the cycle has completed.  (This is an important issue because
Dropbox has a very large cycle in production code.  But I'd like to
deal with it later.)

Additional wrinkles
-------------------

During implementation more wrinkles were found.

- When a submodule of a package (e.g. x.y) is encountered, the parent
  package (e.g. x) must also be loaded, but it is not strictly a
  dependency.  See State.add_ancestors() below.
"""


class ModuleNotFound(Exception):
    """Control flow exception to signal that a module was not found."""


class State:
    """The state for a module.

    The source is only used for the -c command line option; in that
    case path is None.  Otherwise source is None and path isn't.
    """

    manager = None  # type: BuildManager
    order_counter = 0  # type: ClassVar[int]
    order = None  # type: int  # Order in which modules were encountered
    id = None  # type: str  # Fully qualified module name
    path = None  # type: Optional[str]  # Path to module source
    xpath = None  # type: str  # Path or '<string>'
    source = None  # type: Optional[str]  # Module source code
    source_hash = None  # type: Optional[str]  # Hash calculated based on the source code
    meta_source_hash = None  # type: Optional[str]  # Hash of the source given in the meta, if any
    meta = None  # type: Optional[CacheMeta]
    data = None  # type: Optional[str]
    tree = None  # type: Optional[MypyFile]
    dependencies = None  # type: List[str]  # Modules directly imported by the module
    suppressed = None  # type: List[str]  # Suppressed/missing dependencies
    priorities = None  # type: Dict[str, int]

    # Map each dependency to the line number where it is first imported
    dep_line_map = None  # type: Dict[str, int]

    # Parent package, its parent, etc.
    ancestors = None  # type: Optional[List[str]]

    # A list of all direct submodules of a given module
    child_modules = None  # type: Set[str]

    # List of (path, line number) tuples giving context for import
    import_context = None  # type: List[Tuple[str, int]]

    # The State from which this module was imported, if any
    caller_state = None  # type: Optional[State]

    # If caller_state is set, the line number in the caller where the import occurred
    caller_line = 0

    # If True, indicate that the public interface of this module is unchanged
    externally_same = True

    # Contains a hash of the public interface in incremental mode
    interface_hash = ""  # type: str

    # Options, specialized for this file
    options = None  # type: Options

    # Whether to ignore all errors
    ignore_all = False

    # Whether the module has an error or any of its dependencies have one.
    transitive_error = False

    fine_grained_deps = None  # type: Dict[str, Set[str]]

    # Type checker used for checking this file.  Use type_checker() for
    # access and to construct this on demand.
    _type_checker = None  # type: Optional[TypeChecker]

    def __init__(self,
                 id: Optional[str],
                 path: Optional[str],
                 source: Optional[str],
                 manager: BuildManager,
                 caller_state: 'Optional[State]' = None,
                 caller_line: int = 0,
                 ancestor_for: 'Optional[State]' = None,
                 root_source: bool = False,
                 # If `temporary` is True, this State is being created to just
                 # quickly parse/load the tree, without an intention to further
                 # process it. With this flag, any changes to external state as well
                 # as error reporting should be avoided.
                 temporary: bool = False,
                 ) -> None:
        assert id or path or source is not None, "Neither id, path nor source given"
        self.manager = manager
        State.order_counter += 1
        self.order = State.order_counter
        self.caller_state = caller_state
        self.caller_line = caller_line
        if caller_state:
            self.import_context = caller_state.import_context[:]
            self.import_context.append((caller_state.xpath, caller_line))
        else:
            self.import_context = []
        self.id = id or '__main__'
        self.options = manager.options.clone_for_module(self.id)
        self._type_checker = None
        self.fine_grained_deps = {}
        if not path and source is None:
            assert id is not None
            try:
                path, follow_imports = find_module_and_diagnose(
                    manager, id, self.options, caller_state, caller_line,
                    ancestor_for, root_source, skip_diagnose=temporary)
            except ModuleNotFound:
                if not temporary:
                    manager.missing_modules.add(id)
                raise
            if follow_imports == 'silent':
                self.ignore_all = True
        self.path = path
        self.xpath = path or '<string>'
        if path and source is None and self.manager.fscache.isdir(path):
            source = ''
        self.source = source
        if path and source is None and self.manager.cache_enabled:
            self.meta = find_cache_meta(self.id, path, manager)
            # TODO: Get mtime if not cached.
            if self.meta is not None:
                self.interface_hash = self.meta.interface_hash
                self.meta_source_hash = self.meta.hash
        self.add_ancestors()
        self.meta = validate_meta(self.meta, self.id, self.path, self.ignore_all, manager)
        if self.meta:
            # Make copies, since we may modify these and want to
            # compare them to the originals later.
            self.dependencies = list(self.meta.dependencies)
            self.suppressed = list(self.meta.suppressed)
            all_deps = self.dependencies + self.suppressed
            assert len(all_deps) == len(self.meta.dep_prios)
            self.priorities = {id: pri
                               for id, pri in zip(all_deps, self.meta.dep_prios)}
            assert len(all_deps) == len(self.meta.dep_lines)
            self.dep_line_map = {id: line
                                 for id, line in zip(all_deps, self.meta.dep_lines)}
            self.child_modules = set(self.meta.child_modules)
            if temporary:
                self.load_tree(temporary=True)
            if not manager.use_fine_grained_cache():
                # Special case: if there were a previously missing package imported here
                # and it is not present, then we need to re-calculate dependencies.
                # This is to support patterns like this:
                #     from missing_package import missing_module  # type: ignore
                # At first mypy doesn't know that `missing_module` is a module
                # (it may be a variable, a class, or a function), so it is not added to
                # suppressed dependencies. Therefore, when the package with module is added,
                # we need to re-calculate dependencies.
                # NOTE: see comment below for why we skip this in fine grained mode.
                if exist_added_packages(self.suppressed, manager, self.options):
                    self.parse_file()  # This is safe because the cache is anyway stale.
                    self.compute_dependencies()
        else:
            # When doing a fine-grained cache load, pretend we only
            # know about modules that have cache information and defer
            # handling new modules until the fine-grained update.
            if manager.use_fine_grained_cache():
                manager.log("Deferring module to fine-grained update %s (%s)" % (path, id))
                raise ModuleNotFound

            # Parse the file (and then some) to get the dependencies.
            self.parse_file()
            self.compute_dependencies()
            self.child_modules = set()

    @property
    def xmeta(self) -> CacheMeta:
        assert self.meta, "missing meta on allegedly fresh module"
        return self.meta

    def add_ancestors(self) -> None:
        if self.path is not None:
            _, name = os.path.split(self.path)
            base, _ = os.path.splitext(name)
            if '.' in base:
                # This is just a weird filename, don't add anything
                self.ancestors = []
                return
        # All parent packages are new ancestors.
        ancestors = []
        parent = self.id
        while '.' in parent:
            parent, _ = parent.rsplit('.', 1)
            ancestors.append(parent)
        self.ancestors = ancestors

    def is_fresh(self) -> bool:
        """Return whether the cache data for this file is fresh."""
        # NOTE: self.dependencies may differ from
        # self.meta.dependencies when a dependency is dropped due to
        # suppression by silent mode.  However when a suppressed
        # dependency is added back we find out later in the process.
        return (self.meta is not None
                and self.is_interface_fresh()
                and self.dependencies == self.meta.dependencies
                and self.child_modules == set(self.meta.child_modules))

    def is_interface_fresh(self) -> bool:
        return self.externally_same

    def mark_as_rechecked(self) -> None:
        """Marks this module as having been fully re-analyzed by the type-checker."""
        self.manager.rechecked_modules.add(self.id)

    def mark_interface_stale(self, *, on_errors: bool = False) -> None:
        """Marks this module as having a stale public interface, and discards the cache data."""
        self.externally_same = False
        if not on_errors:
            self.manager.stale_modules.add(self.id)

    def check_blockers(self) -> None:
        """Raise CompileError if a blocking error is detected."""
        if self.manager.errors.is_blockers():
            self.manager.log("Bailing due to blocking errors")
            self.manager.errors.raise_error()

    @contextlib.contextmanager
    def wrap_context(self) -> Iterator[None]:
        save_import_context = self.manager.errors.import_context()
        self.manager.errors.set_import_context(self.import_context)
        try:
            yield
        except CompileError:
            raise
        except Exception as err:
            report_internal_error(err, self.path, 0, self.manager.errors, self.options)
        self.manager.errors.set_import_context(save_import_context)
        self.check_blockers()

    # Methods for processing cached modules.
    def load_fine_grained_deps(self) -> None:
        assert self.meta is not None, "Internal error: this method must be called only" \
                                      " for cached modules"
        assert self.meta.deps_json
        deps = json.loads(self.manager.metastore.read(self.meta.deps_json))
        # TODO: Assert deps file wasn't changed.
        self.fine_grained_deps = {k: set(v) for k, v in deps.items()}

    def load_tree(self, temporary: bool = False) -> None:
        assert self.meta is not None, "Internal error: this method must be called only" \
                                      " for cached modules"
        data = json.loads(self.manager.metastore.read(self.meta.data_json))
        # TODO: Assert data file wasn't changed.
        self.tree = MypyFile.deserialize(data)
        if not temporary:
            self.manager.modules[self.id] = self.tree
            self.manager.add_stats(fresh_trees=1)

    def fix_cross_refs(self) -> None:
        assert self.tree is not None, "Internal error: method must be called on parsed file only"
        # We need to set quick_and_dirty when doing a fine grained
        # cache load because we need to gracefully handle missing modules.
        fixup_module(self.tree, self.manager.modules,
                     self.manager.options.quick_and_dirty or
                     self.options.use_fine_grained_cache)

    def patch_dependency_parents(self) -> None:
        """
        In Python, if a and a.b are both modules, running `import a.b` will
        modify not only the current module's namespace, but a's namespace as
        well -- see SemanticAnalyzerPass2.add_submodules_to_parent_modules for more
        details.

        However, this patching process can occur after `a` has been parsed and
        serialized during increment mode. Consequently, we need to repeat this
        patch when deserializing a cached file.

        This function should be called only when processing fresh SCCs -- the
        semantic analyzer will perform this patch for us when processing stale
        SCCs.
        """
        for dep in self.dependencies:
            self.manager.semantic_analyzer.add_submodules_to_parent_modules(dep, True)

    def fix_suppressed_dependencies(self, graph: Graph) -> None:
        """Corrects whether dependencies are considered stale in silent mode.

        This method is a hack to correct imports in silent mode + incremental mode.
        In particular, the problem is that when running mypy with a cold cache, the
        `parse_file(...)` function is called *at the start* of the `load_graph(...)` function.
        Note that load_graph will mark some dependencies as suppressed if they weren't specified
        on the command line in silent mode.

        However, if the interface for a module is changed, parse_file will be called within
        `process_stale_scc` -- *after* load_graph is finished, wiping out the changes load_graph
        previously made.

        This method is meant to be run after parse_file finishes in process_stale_scc and will
        recompute what modules should be considered suppressed in silent mode.
        """
        # TODO: See if it's possible to move this check directly into parse_file in some way.
        # TODO: Find a way to write a test case for this fix.
        # TODO: I suspect that splitting compute_dependencies() out from parse_file
        # obviates the need for this but lacking a test case for the problem this fixed...
        silent_mode = (self.options.ignore_missing_imports or
                       self.options.follow_imports == 'skip')
        if not silent_mode:
            return

        new_suppressed = []
        new_dependencies = []
        entry_points = self.manager.source_set.source_modules
        for dep in self.dependencies + self.suppressed:
            ignored = dep in self.suppressed and dep not in entry_points
            if ignored or dep not in graph:
                new_suppressed.append(dep)
            else:
                new_dependencies.append(dep)
        self.dependencies = new_dependencies
        self.suppressed = new_suppressed

    # Methods for processing modules from source code.

    def parse_file(self) -> None:
        """Parse file and run first pass of semantic analysis.

        Everything done here is local to the file. Don't depend on imported
        modules in any way. Also record module dependencies based on imports.
        """
        if self.tree is not None:
            # The file was already parsed (in __init__()).
            return

        manager = self.manager
        modules = manager.modules
        manager.log("Parsing %s (%s)" % (self.xpath, self.id))

        with self.wrap_context():
            source = self.source
            self.source = None  # We won't need it again.
            if self.path and source is None:
                try:
                    path = manager.maybe_swap_for_shadow_path(self.path)
                    source = decode_python_encoding(manager.fscache.read(path),
                                                    manager.options.python_version)
                    self.source_hash = manager.fscache.md5(path)
                except IOError as ioerr:
                    # ioerr.strerror differs for os.stat failures between Windows and
                    # other systems, but os.strerror(ioerr.errno) does not, so we use that.
                    # (We want the error messages to be platform-independent so that the
                    # tests have predictable output.)
                    raise CompileError([
                        "mypy: can't read file '{}': {}".format(
                            self.path, os.strerror(ioerr.errno))])
                except (UnicodeDecodeError, DecodeError) as decodeerr:
                    raise CompileError([
                        "mypy: can't decode file '{}': {}".format(self.path, str(decodeerr))])
            else:
                assert source is not None
                self.source_hash = compute_hash(source)
            self.tree = manager.parse_file(self.id, self.xpath, source,
                                           self.ignore_all or self.options.ignore_errors)

        modules[self.id] = self.tree

        # Do the first pass of semantic analysis: add top-level
        # definitions in the file to the symbol table.  We must do
        # this before processing imports, since this may mark some
        # import statements as unreachable.
        first = SemanticAnalyzerPass1(manager.semantic_analyzer)
        with self.wrap_context():
            first.visit_file(self.tree, self.xpath, self.id, self.options)

        # Initialize module symbol table, which was populated by the
        # semantic analyzer.
        # TODO: Why can't SemanticAnalyzerPass1 .analyze() do this?
        self.tree.names = manager.semantic_analyzer.globals

        self.check_blockers()

    def compute_dependencies(self) -> None:
        """Compute a module's dependencies after parsing it.

        This is used when we parse a file that we didn't have
        up-to-date cache information for. When we have an up-to-date
        cache, we just use the cached info.
        """
        manager = self.manager
        assert self.tree is not None

        # Compute (direct) dependencies.
        # Add all direct imports (this is why we needed the first pass).
        # Also keep track of each dependency's source line.
        dependencies = []
        priorities = {}  # type: Dict[str, int]  # id -> priority
        dep_line_map = {}  # type: Dict[str, int]  # id -> line
        for pri, id, line in manager.all_imported_modules_in_file(self.tree):
            priorities[id] = min(pri, priorities.get(id, PRI_ALL))
            if id == self.id:
                continue
            if id not in dep_line_map:
                dependencies.append(id)
                dep_line_map[id] = line
        # Every module implicitly depends on builtins.
        if self.id != 'builtins' and 'builtins' not in dep_line_map:
            dependencies.append('builtins')

        # Missing dependencies will be moved from dependencies to
        # suppressed when they fail to be loaded in load_graph.
        self.dependencies = dependencies
        self.suppressed = []
        self.priorities = priorities
        self.dep_line_map = dep_line_map

        self.check_blockers()  # Can fail due to bogus relative imports

    def semantic_analysis(self) -> None:
        assert self.tree is not None, "Internal error: method must be called on parsed file only"
        patches = []  # type: List[Tuple[int, Callable[[], None]]]
        with self.wrap_context():
            self.manager.semantic_analyzer.visit_file(self.tree, self.xpath, self.options, patches)
        self.patches = patches

    def semantic_analysis_pass_three(self) -> None:
        assert self.tree is not None, "Internal error: method must be called on parsed file only"
        patches = []  # type: List[Tuple[int, Callable[[], None]]]
        with self.wrap_context():
            self.manager.semantic_analyzer_pass3.visit_file(self.tree, self.xpath,
                                                            self.options, patches)
            if self.options.dump_type_stats:
                dump_type_stats(self.tree, self.xpath)
        self.patches = patches + self.patches

    def semantic_analysis_apply_patches(self) -> None:
        apply_semantic_analyzer_patches(self.patches)

    def type_check_first_pass(self) -> None:
        if self.options.semantic_analysis_only:
            return
        with self.wrap_context():
            self.type_checker().check_first_pass()

    def type_checker(self) -> TypeChecker:
        if not self._type_checker:
            assert self.tree is not None, "Internal error: must be called on parsed file only"
            manager = self.manager
            self._type_checker = TypeChecker(manager.errors, manager.modules, self.options,
                                             self.tree, self.xpath, manager.plugin)
        return self._type_checker

    def type_map(self) -> Dict[Expression, Type]:
        return self.type_checker().type_map

    def type_check_second_pass(self) -> bool:
        if self.options.semantic_analysis_only:
            return False
        with self.wrap_context():
            return self.type_checker().check_second_pass()

    def finish_passes(self) -> None:
        assert self.tree is not None, "Internal error: method must be called on parsed file only"
        manager = self.manager
        if self.options.semantic_analysis_only:
            return
        with self.wrap_context():
            # Some tests (and tools) want to look at the set of all types.
            options = manager.options
            if options.export_types:
                manager.all_types.update(self.type_map())

            # We should always patch indirect dependencies, even in full (non-incremental) builds,
            # because the cache still may be written, and it must be correct.
            self._patch_indirect_dependencies(self.type_checker().module_refs, self.type_map())

            if self.options.dump_inference_stats:
                dump_type_stats(self.tree, self.xpath, inferred=True,
                                typemap=self.type_map())
            manager.report_file(self.tree, self.type_map(), self.options)

    def _patch_indirect_dependencies(self,
                                     module_refs: Set[str],
                                     type_map: Dict[Expression, Type]) -> None:
        types = set(type_map.values())
        assert None not in types
        valid = self.valid_references()

        encountered = self.manager.indirection_detector.find_modules(types) | module_refs
        extra = encountered - valid

        for dep in sorted(extra):
            if dep not in self.manager.modules:
                continue
            if dep not in self.suppressed and dep not in self.manager.missing_modules:
                self.dependencies.append(dep)
                self.priorities[dep] = PRI_INDIRECT
            elif dep not in self.suppressed and dep in self.manager.missing_modules:
                self.suppressed.append(dep)

    def compute_fine_grained_deps(self) -> None:
        assert self.tree is not None
        if '/typeshed/' in self.xpath or self.xpath.startswith('typeshed/'):
            # We don't track changes to typeshed -- the assumption is that they are only changed
            # as part of mypy updates, which will invalidate everything anyway.
            #
            # TODO: Not a reliable test, as we could have a package named typeshed.
            # TODO: Consider relaxing this -- maybe allow some typeshed changes to be tracked.
            return
        self.fine_grained_deps = get_dependencies(target=self.tree,
                                                  type_map=self.type_map(),
                                                  python_version=self.options.python_version,
                                                  options=self.manager.options)

    def valid_references(self) -> Set[str]:
        assert self.ancestors is not None
        valid_refs = set(self.dependencies + self.suppressed + self.ancestors)
        valid_refs.add(self.id)

        if "os" in valid_refs:
            valid_refs.add("os.path")

        return valid_refs

    def write_cache(self) -> None:
        assert self.tree is not None, "Internal error: method must be called on parsed file only"
        # We don't support writing cache files in fine-grained incremental mode.
        if (not self.path
                or self.options.cache_dir == os.devnull
                or self.options.fine_grained_incremental):
            return
        if self.manager.options.quick_and_dirty:
            is_errors = self.manager.errors.is_errors_for_file(self.path)
        else:
            is_errors = self.transitive_error
        if is_errors:
            delete_cache(self.id, self.path, self.manager)
            self.meta = None
            self.mark_interface_stale(on_errors=True)
            return
        dep_prios = self.dependency_priorities()
        dep_lines = self.dependency_lines()
        assert self.source_hash is not None
        new_interface_hash, self.meta = write_cache(
            self.id, self.path, self.tree,
            {k: list(v) for k, v in self.fine_grained_deps.items()},
            list(self.dependencies), list(self.suppressed), list(self.child_modules),
            dep_prios, dep_lines, self.interface_hash, self.source_hash, self.ignore_all,
            self.manager)
        if new_interface_hash == self.interface_hash:
            self.manager.log("Cached module {} has same interface".format(self.id))
        else:
            self.manager.log("Cached module {} has changed interface".format(self.id))
            self.mark_interface_stale()
            self.interface_hash = new_interface_hash

    def verify_dependencies(self, suppressed_only: bool = False) -> None:
        """Report errors for import targets in modules that don't exist.

        If suppressed_only is set, only check suppressed dependencies.
        """
        manager = self.manager
        assert self.ancestors is not None
        if suppressed_only:
            all_deps = self.suppressed
        else:
            # Strip out indirect dependencies. See comment in build.load_graph().
            dependencies = [dep for dep in self.dependencies
                            if self.priorities.get(dep) != PRI_INDIRECT]
            all_deps = dependencies + self.suppressed + self.ancestors
        for dep in all_deps:
            if dep in manager.modules:
                continue
            options = manager.options.clone_for_module(dep)
            if options.ignore_missing_imports:
                continue
            line = self.dep_line_map.get(dep, 1)
            try:
                if dep in self.ancestors:
                    state, ancestor = None, self  # type: (Optional[State], Optional[State])
                else:
                    state, ancestor = self, None
                # Called just for its side effects of producing diagnostics.
                find_module_and_diagnose(
                    manager, dep, options,
                    caller_state=state, caller_line=line,
                    ancestor_for=ancestor)
            except (ModuleNotFound, CompileError):
                # Swallow up any ModuleNotFounds or CompilerErrors while generating
                # a diagnostic. CompileErrors may get generated in
                # fine-grained mode when an __init__.py is deleted, if a module
                # that was in that package has targets reprocessed before
                # it is renamed.
                pass

    def dependency_priorities(self) -> List[int]:
        return [self.priorities.get(dep, PRI_HIGH) for dep in self.dependencies + self.suppressed]

    def dependency_lines(self) -> List[int]:
        return [self.dep_line_map.get(dep, 1) for dep in self.dependencies + self.suppressed]

    def generate_unused_ignore_notes(self) -> None:
        if self.options.warn_unused_ignores:
            # If this file was initially loaded from the cache, it may have suppressed
            # dependencies due to imports with ignores on them. We need to generate
            # those errors to avoid spuriously flagging them as unused ignores.
            if self.meta:
                self.verify_dependencies(suppressed_only=True)
            self.manager.errors.generate_unused_ignore_notes(self.xpath)


# Module import and diagnostic glue


def find_module_and_diagnose(manager: BuildManager,
                             id: str,
                             options: Options,
                             caller_state: 'Optional[State]' = None,
                             caller_line: int = 0,
                             ancestor_for: 'Optional[State]' = None,
                             root_source: bool = False,
                             skip_diagnose: bool = False) -> Tuple[str, str]:
    """Find a module by name, respecting follow_imports and producing diagnostics.

    If the module is not found, then the ModuleNotFound exception is raised.

    Args:
      id: module to find
      options: the options for the module being loaded
      caller_state: the state of the importing module, if applicable
      caller_line: the line number of the import
      ancestor_for: the child module this is an ancestor of, if applicable
      root_source: whether this source was specified on the command line
      skip_diagnose: skip any error diagnosis and reporting (but ModuleNotFound is
          still raised if the module is missing)

    The specified value of follow_imports for a module can be overridden
    if the module is specified on the command line or if it is a stub,
    so we compute and return the "effective" follow_imports of the module.

    Returns a tuple containing (file path, target's effective follow_imports setting)
    """
    file_id = id
    if id == 'builtins' and options.python_version[0] == 2:
        # The __builtin__ module is called internally by mypy
        # 'builtins' in Python 2 mode (similar to Python 3),
        # but the stub file is __builtin__.pyi.  The reason is
        # that a lot of code hard-codes 'builtins.x' and it's
        # easier to work it around like this.  It also means
        # that the implementation can mostly ignore the
        # difference and just assume 'builtins' everywhere,
        # which simplifies code.
        file_id = '__builtin__'
    path = manager.find_module_cache.find_module(file_id)
    if path:
        # For non-stubs, look at options.follow_imports:
        # - normal (default) -> fully analyze
        # - silent -> analyze but silence errors
        # - skip -> don't analyze, make the type Any
        follow_imports = options.follow_imports
        if (root_source  # Honor top-level modules
                or (not path.endswith('.py')  # Stubs are always normal
                    and not options.follow_imports_for_stubs)  # except when they aren't
                or id == 'builtins'):  # Builtins is always normal
            follow_imports = 'normal'
        if skip_diagnose:
            pass
        elif follow_imports == 'silent':
            # Still import it, but silence non-blocker errors.
            manager.log("Silencing %s (%s)" % (path, id))
        elif follow_imports == 'skip' or follow_imports == 'error':
            # In 'error' mode, produce special error messages.
            if id not in manager.missing_modules:
                manager.log("Skipping %s (%s)" % (path, id))
            if follow_imports == 'error':
                if ancestor_for:
                    skipping_ancestor(manager, id, path, ancestor_for)
                else:
                    skipping_module(manager, caller_line, caller_state,
                                    id, path)
            raise ModuleNotFound
        if not manager.options.no_silence_site_packages:
            for dir in manager.search_paths.package_path + manager.search_paths.typeshed_path:
                if is_sub_path(path, dir):
                    # Silence errors in site-package dirs and typeshed
                    follow_imports = 'silent'
        return (path, follow_imports)
    else:
        # Could not find a module.  Typically the reason is a
        # misspelled module name, missing stub, module not in
        # search path or the module has not been installed.
        if skip_diagnose:
            raise ModuleNotFound
        if caller_state:
            if not (options.ignore_missing_imports or in_partial_package(id, manager)):
                module_not_found(manager, caller_line, caller_state, id)
            raise ModuleNotFound
        elif root_source:
            # If we can't find a root source it's always fatal.
            # TODO: This might hide non-fatal errors from
            # root sources processed earlier.
            raise CompileError(["mypy: can't find module '%s'" % id])
        else:
            raise ModuleNotFound


def exist_added_packages(suppressed: List[str],
                        manager: BuildManager, options: Options) -> bool:
    """Find if there are any newly added packages that were previously suppressed.

    Exclude everything not in build for follow-imports=skip.
    """
    for dep in suppressed:
        if dep in manager.source_set.source_modules:
            # We don't need to add any special logic for this. If a module
            # is added to build, importers will be invalidated by normal mechanism.
            continue
        path = find_module_simple(dep, manager)
        if not path:
            continue
        if (options.follow_imports == 'skip' and
                (not path.endswith('.pyi') or options.follow_imports_for_stubs)):
            continue
        if '__init__.py' in path:
            # It is better to have a bit lenient test, this will only slightly reduce
            # performance, while having a too strict test may affect correctness.
            return True
    return False


def find_module_simple(id: str, manager: BuildManager) -> Optional[str]:
    """Find a filesystem path for module `id` or `None` if not found."""
    return manager.find_module_cache.find_module(id)


def in_partial_package(id: str, manager: BuildManager) -> bool:
    """Check if a missing module can potentially be a part of a package.

    This checks if there is any existing parent __init__.pyi stub that
    defines a module-level __getattr__ (a.k.a. partial stub package).
    """
    while '.' in id:
        parent, _ = id.rsplit('.', 1)
        if parent in manager.modules:
            parent_mod = manager.modules[parent]  # type: Optional[MypyFile]
        else:
            # Parent is not in build, try quickly if we can find it.
            try:
                parent_st = State(id=parent, path=None, source=None, manager=manager,
                                  temporary=True)
            except (ModuleNotFound, CompileError):
                parent_mod = None
            else:
                parent_mod = parent_st.tree
        if parent_mod is not None:
            if parent_mod.is_partial_stub_package:
                return True
            else:
                # Bail out soon, complete subpackage found
                return False
        id = parent
    return False


def module_not_found(manager: BuildManager, line: int, caller_state: State,
                     target: str) -> None:
    errors = manager.errors
    save_import_context = errors.import_context()
    errors.set_import_context(caller_state.import_context)
    errors.set_file(caller_state.xpath, caller_state.id)
    stub_msg = "(Stub files are from https://github.com/python/typeshed)"
    if target == 'builtins':
        errors.report(line, 0, "Cannot find 'builtins' module. Typeshed appears broken!",
                      blocker=True)
        errors.raise_error()
    elif ((manager.options.python_version[0] == 2 and moduleinfo.is_py2_std_lib_module(target))
          or (manager.options.python_version[0] >= 3
              and moduleinfo.is_py3_std_lib_module(target))):
        errors.report(
            line, 0, "No library stub file for standard library module '{}'".format(target))
        errors.report(line, 0, stub_msg, severity='note', only_once=True)
    elif moduleinfo.is_third_party_module(target):
        errors.report(line, 0, "No library stub file for module '{}'".format(target))
        errors.report(line, 0, stub_msg, severity='note', only_once=True)
    else:
        note = "See https://mypy.readthedocs.io/en/latest/running_mypy.html#missing-imports"
        errors.report(line, 0, "Cannot find module named '{}'".format(target))
        errors.report(line, 0, note, severity='note', only_once=True)
    errors.set_import_context(save_import_context)


def skipping_module(manager: BuildManager, line: int, caller_state: Optional[State],
                    id: str, path: str) -> None:
    """Produce an error for an import ignored due to --follow_imports=error"""
    assert caller_state, (id, path)
    save_import_context = manager.errors.import_context()
    manager.errors.set_import_context(caller_state.import_context)
    manager.errors.set_file(caller_state.xpath, caller_state.id)
    manager.errors.report(line, 0,
                          "Import of '%s' ignored" % (id,),
                          severity='note')
    manager.errors.report(line, 0,
                          "(Using --follow-imports=error, module not passed on command line)",
                          severity='note', only_once=True)
    manager.errors.set_import_context(save_import_context)


def skipping_ancestor(manager: BuildManager, id: str, path: str, ancestor_for: 'State') -> None:
    """Produce an error for an ancestor ignored due to --follow_imports=error"""
    # TODO: Read the path (the __init__.py file) and return
    # immediately if it's empty or only contains comments.
    # But beware, some package may be the ancestor of many modules,
    # so we'd need to cache the decision.
    manager.errors.set_import_context([])
    manager.errors.set_file(ancestor_for.xpath, ancestor_for.id)
    manager.errors.report(-1, -1, "Ancestor package '%s' ignored" % (id,),
                          severity='note', only_once=True)
    manager.errors.report(-1, -1,
                          "(Using --follow-imports=error, submodule passed on command line)",
                          severity='note', only_once=True)


# The driver


def dispatch(sources: List[BuildSource], manager: BuildManager) -> Graph:
    manager.log()
    manager.log("Mypy version %s" % __version__)
    t0 = time.time()
    graph = load_graph(sources, manager)

    # This is a kind of unfortunate hack to work around some of fine-grained's
    # fragility: if we have loaded less than 50% of the specified files from
    # cache in fine-grained cache mode, load the graph again honestly.
    # In this case, we just turn the cache off entirely, so we don't need
    # to worry about some files being loaded and some from cache and so
    # that fine-grained mode never *writes* to the cache.
    if manager.use_fine_grained_cache() and len(graph) < 0.50 * len(sources):
        manager.log("Redoing load_graph without cache because too much was missing")
        manager.cache_enabled = False
        graph = load_graph(sources, manager)

    t1 = time.time()
    manager.add_stats(graph_size=len(graph),
                      stubs_found=sum(g.path is not None and g.path.endswith('.pyi')
                                      for g in graph.values()),
                      graph_load_time=(t1 - t0),
                      fm_cache_size=len(manager.find_module_cache.results),
                      fm_dir_cache_size=len(manager.find_module_cache.dirs),
                      )
    if not graph:
        print("Nothing to do?!")
        return graph
    manager.log("Loaded graph with %d nodes (%.3f sec)" % (len(graph), t1 - t0))
    if manager.options.dump_graph:
        dump_graph(graph)
        return graph

    # Fine grained protocol dependencies are serialized separately, so we read them
    # after we load the cache for whole graph.
    # We need to read them both for running in daemon mode and if we are generating
    # a fine-grained cache (so that we can properly update them incrementally).
    # The `read_protocol_cache` will also validate
    # the protocol cache against the loaded individual cache files.
    if manager.options.cache_fine_grained or manager.use_fine_grained_cache():
        proto_deps = read_protocol_cache(manager, graph)
        if proto_deps is not None:
            TypeState.proto_deps = proto_deps
        elif manager.stats.get('fresh_metas', 0) > 0:
            # Clear the stats so we don't infinite loop because of positive fresh_metas
            manager.stats.clear()
            # There were some cache files read, but no protocol dependencies loaded.
            manager.log("Error reading protocol dependencies cache -- aborting cache load")
            manager.cache_enabled = False
            manager.log("Falling back to full run -- reloading graph...")
            return dispatch(sources, manager)

    # If we are loading a fine-grained incremental mode cache, we
    # don't want to do a real incremental reprocess of the graph---we
    # just want to load in all of the cache information.
    if manager.use_fine_grained_cache():
        process_fine_grained_cache_graph(graph, manager)
    else:
        process_graph(graph, manager)
        # Update plugins snapshot.
        write_plugins_snapshot(manager)
        manager.old_plugins_snapshot = manager.plugins_snapshot
        if manager.options.cache_fine_grained or manager.options.fine_grained_incremental:
            # If we are running a daemon or are going to write cache for further fine grained use,
            # then we need to collect fine grained protocol dependencies.
            # Since these are a global property of the program, they are calculated after we
            # processed the whole graph.
            TypeState.update_protocol_deps()
            if TypeState.proto_deps is not None and not manager.options.fine_grained_incremental:
                write_protocol_deps_cache(TypeState.proto_deps, manager, graph)

    if manager.options.dump_deps:
        # This speeds up startup a little when not using the daemon mode.
        from mypy.server.deps import dump_all_dependencies
        dump_all_dependencies(manager.modules, manager.all_types,
                              manager.options.python_version, manager.options)
    return graph


class NodeInfo:
    """Some info about a node in the graph of SCCs."""

    def __init__(self, index: int, scc: List[str]) -> None:
        self.node_id = "n%d" % index
        self.scc = scc
        self.sizes = {}  # type: Dict[str, int]  # mod -> size in bytes
        self.deps = {}  # type: Dict[str, int]  # node_id -> pri

    def dumps(self) -> str:
        """Convert to JSON string."""
        total_size = sum(self.sizes.values())
        return "[%s, %s, %s,\n     %s,\n     %s]" % (json.dumps(self.node_id),
                                                     json.dumps(total_size),
                                                     json.dumps(self.scc),
                                                     json.dumps(self.sizes),
                                                     json.dumps(self.deps))


def dump_graph(graph: Graph) -> None:
    """Dump the graph as a JSON string to stdout.

    This copies some of the work by process_graph()
    (sorted_components() and order_ascc()).
    """
    nodes = []
    sccs = sorted_components(graph)
    for i, ascc in enumerate(sccs):
        scc = order_ascc(graph, ascc)
        node = NodeInfo(i, scc)
        nodes.append(node)
    inv_nodes = {}  # module -> node_id
    for node in nodes:
        for mod in node.scc:
            inv_nodes[mod] = node.node_id
    for node in nodes:
        for mod in node.scc:
            state = graph[mod]
            size = 0
            if state.path:
                try:
                    size = os.path.getsize(state.path)
                except os.error:
                    pass
            node.sizes[mod] = size
            for dep in state.dependencies:
                if dep in state.priorities:
                    pri = state.priorities[dep]
                    if dep in inv_nodes:
                        dep_id = inv_nodes[dep]
                        if (dep_id != node.node_id and
                                (dep_id not in node.deps or pri < node.deps[dep_id])):
                            node.deps[dep_id] = pri
    print("[" + ",\n ".join(node.dumps() for node in nodes) + "\n]")


def load_graph(sources: List[BuildSource], manager: BuildManager,
               old_graph: Optional[Graph] = None,
               new_modules: Optional[List[State]] = None) -> Graph:
    """Given some source files, load the full dependency graph.

    If an old_graph is passed in, it is used as the starting point and
    modified during graph loading.

    If a new_modules is passed in, any modules that are loaded are
    added to the list. This is an argument and not a return value
    so that the caller can access it even if load_graph fails.

    As this may need to parse files, this can raise CompileError in case
    there are syntax errors.
    """

    graph = old_graph if old_graph is not None else {}  # type: Graph

    # The deque is used to implement breadth-first traversal.
    # TODO: Consider whether to go depth-first instead.  This may
    # affect the order in which we process files within import cycles.
    new = new_modules if new_modules is not None else []
    entry_points = set()  # type: Set[str]
    # Seed the graph with the initial root sources.
    for bs in sources:
        try:
            st = State(id=bs.module, path=bs.path, source=bs.text, manager=manager,
                       root_source=True)
        except ModuleNotFound:
            continue
        if st.id in graph:
            manager.errors.set_file(st.xpath, st.id)
            manager.errors.report(-1, -1, "Duplicate module named '%s'" % st.id)
            manager.errors.raise_error()
        graph[st.id] = st
        new.append(st)
        entry_points.add(bs.module)
    # Collect dependencies.  We go breadth-first.
    # More nodes might get added to new as we go, but that's fine.
    for st in new:
        assert st.ancestors is not None
        # Strip out indirect dependencies.  These will be dealt with
        # when they show up as direct dependencies, and there's a
        # scenario where they hurt:
        # - Suppose A imports B and B imports C.
        # - Suppose on the next round:
        #   - C is deleted;
        #   - B is updated to remove the dependency on C;
        #   - A is unchanged.
        # - In this case A's cached *direct* dependencies are still valid
        #   (since direct dependencies reflect the imports found in the source)
        #   but A's cached *indirect* dependency on C is wrong.
        dependencies = [dep for dep in st.dependencies if st.priorities.get(dep) != PRI_INDIRECT]
        if not manager.use_fine_grained_cache():
            # TODO: Ideally we could skip here modules that appeared in st.suppressed
            # because they are not in build with `follow-imports=skip`.
            # This way we could avoid overhead of cloning options in `State.__init__()`
            # below to get the option value. This is quite minor performance loss however.
            added = [dep for dep in st.suppressed if find_module_simple(dep, manager)]
        else:
            # During initial loading we don't care about newly added modules,
            # they will be taken care of during fine grained update. See also
            # comment about this in `State.__init__()`.
            added = []
        for dep in st.ancestors + dependencies + st.suppressed:
            ignored = dep in st.suppressed and dep not in entry_points
            if ignored and dep not in added:
                manager.missing_modules.add(dep)
            elif dep not in graph:
                try:
                    if dep in st.ancestors:
                        # TODO: Why not 'if dep not in st.dependencies' ?
                        # Ancestors don't have import context.
                        newst = State(id=dep, path=None, source=None, manager=manager,
                                      ancestor_for=st)
                    else:
                        newst = State(id=dep, path=None, source=None, manager=manager,
                                      caller_state=st, caller_line=st.dep_line_map.get(dep, 1))
                except ModuleNotFound:
                    if dep in st.dependencies:
                        st.dependencies.remove(dep)
                        st.suppressed.append(dep)
                else:
                    assert newst.id not in graph, newst.id
                    graph[newst.id] = newst
                    new.append(newst)
            if dep in st.ancestors and dep in graph:
                graph[dep].child_modules.add(st.id)
            if dep in graph and dep in st.suppressed:
                # Previously suppressed file is now visible
                if dep in st.suppressed:
                    st.suppressed.remove(dep)
                    st.dependencies.append(dep)
    return graph


def process_graph(graph: Graph, manager: BuildManager) -> None:
    """Process everything in dependency order."""
    sccs = sorted_components(graph)
    manager.log("Found %d SCCs; largest has %d nodes" %
                (len(sccs), max(len(scc) for scc in sccs)))

    fresh_scc_queue = []  # type: List[List[str]]

    # We're processing SCCs from leaves (those without further
    # dependencies) to roots (those from which everything else can be
    # reached).
    for ascc in sccs:
        # Order the SCC's nodes using a heuristic.
        # Note that ascc is a set, and scc is a list.
        scc = order_ascc(graph, ascc)
        # If builtins is in the list, move it last.  (This is a bit of
        # a hack, but it's necessary because the builtins module is
        # part of a small cycle involving at least {builtins, abc,
        # typing}.  Of these, builtins must be processed last or else
        # some builtin objects will be incompletely processed.)
        if 'builtins' in ascc:
            scc.remove('builtins')
            scc.append('builtins')
        if manager.options.verbosity >= 2:
            for id in scc:
                manager.trace("Priorities for %s:" % id,
                              " ".join("%s:%d" % (x, graph[id].priorities[x])
                                       for x in graph[id].dependencies
                                       if x in ascc and x in graph[id].priorities))
        # Because the SCCs are presented in topological sort order, we
        # don't need to look at dependencies recursively for staleness
        # -- the immediate dependencies are sufficient.
        stale_scc = {id for id in scc if not graph[id].is_fresh()}
        fresh = not stale_scc
        deps = set()
        for id in scc:
            deps.update(graph[id].dependencies)
        deps -= ascc
        stale_deps = {id for id in deps if id in graph and not graph[id].is_interface_fresh()}
        if not manager.options.quick_and_dirty:
            fresh = fresh and not stale_deps
        undeps = set()
        if fresh:
            # Check if any dependencies that were suppressed according
            # to the cache have been added back in this run.
            # NOTE: Newly suppressed dependencies are handled by is_fresh().
            for id in scc:
                undeps.update(graph[id].suppressed)
            undeps &= graph.keys()
            if undeps:
                fresh = False
        if fresh:
            # All cache files are fresh.  Check that no dependency's
            # cache file is newer than any scc node's cache file.
            oldest_in_scc = min(graph[id].xmeta.data_mtime for id in scc)
            viable = {id for id in stale_deps if graph[id].meta is not None}
            newest_in_deps = 0 if not viable else max(graph[dep].xmeta.data_mtime
                                                      for dep in viable)
            if manager.options.verbosity >= 3:  # Dump all mtimes for extreme debugging.
                all_ids = sorted(ascc | viable, key=lambda id: graph[id].xmeta.data_mtime)
                for id in all_ids:
                    if id in scc:
                        if graph[id].xmeta.data_mtime < newest_in_deps:
                            key = "*id:"
                        else:
                            key = "id:"
                    else:
                        if graph[id].xmeta.data_mtime > oldest_in_scc:
                            key = "+dep:"
                        else:
                            key = "dep:"
                    manager.trace(" %5s %.0f %s" % (key, graph[id].xmeta.data_mtime, id))
            # If equal, give the benefit of the doubt, due to 1-sec time granularity
            # (on some platforms).
            if manager.options.quick_and_dirty and stale_deps:
                fresh_msg = "fresh(ish)"
            elif oldest_in_scc < newest_in_deps:
                fresh = False
                fresh_msg = "out of date by %.0f seconds" % (newest_in_deps - oldest_in_scc)
            else:
                fresh_msg = "fresh"
        elif undeps:
            fresh_msg = "stale due to changed suppression (%s)" % " ".join(sorted(undeps))
        elif stale_scc:
            fresh_msg = "inherently stale"
            if stale_scc != ascc:
                fresh_msg += " (%s)" % " ".join(sorted(stale_scc))
            if stale_deps:
                fresh_msg += " with stale deps (%s)" % " ".join(sorted(stale_deps))
        else:
            fresh_msg = "stale due to deps (%s)" % " ".join(sorted(stale_deps))

        # Initialize transitive_error for all SCC members from union
        # of transitive_error of dependencies.
        if any(graph[dep].transitive_error for dep in deps if dep in graph):
            for id in scc:
                graph[id].transitive_error = True

        scc_str = " ".join(scc)
        if fresh:
            manager.trace("Queuing %s SCC (%s)" % (fresh_msg, scc_str))
            fresh_scc_queue.append(scc)
        else:
            if len(fresh_scc_queue) > 0:
                manager.log("Processing {} queued fresh SCCs".format(len(fresh_scc_queue)))
                # Defer processing fresh SCCs until we actually run into a stale SCC
                # and need the earlier modules to be loaded.
                #
                # Note that `process_graph` may end with us not having processed every
                # single fresh SCC. This is intentional -- we don't need those modules
                # loaded if there are no more stale SCCs to be rechecked.
                #
                # Also note we shouldn't have to worry about transitive_error here,
                # since modules with transitive errors aren't written to the cache,
                # and if any dependencies were changed, this SCC would be stale.
                # (Also, in quick_and_dirty mode we don't care about transitive errors.)
                #
                # TODO: see if it's possible to determine if we need to process only a
                # _subset_ of the past SCCs instead of having to process them all.
                for prev_scc in fresh_scc_queue:
                    process_fresh_modules(graph, prev_scc, manager)
                fresh_scc_queue = []
            size = len(scc)
            if size == 1:
                manager.log("Processing SCC singleton (%s) as %s" % (scc_str, fresh_msg))
            else:
                manager.log("Processing SCC of size %d (%s) as %s" % (size, scc_str, fresh_msg))
            process_stale_scc(graph, scc, manager)

    sccs_left = len(fresh_scc_queue)
    nodes_left = sum(len(scc) for scc in fresh_scc_queue)
    manager.add_stats(sccs_left=sccs_left, nodes_left=nodes_left)
    if sccs_left:
        manager.log("{} fresh SCCs ({} nodes) left in queue (and will remain unprocessed)"
                    .format(sccs_left, nodes_left))
        manager.trace(str(fresh_scc_queue))
    else:
        manager.log("No fresh SCCs left in queue")


def process_fine_grained_cache_graph(graph: Graph, manager: BuildManager) -> None:
    """Finish loading everything for use in the fine-grained incremental cache"""

    # If we are running in fine-grained incremental mode with caching,
    # we don't actually have much to do: just load the fine-grained
    # deps.
    for id, state in graph.items():
        state.load_fine_grained_deps()


def order_ascc(graph: Graph, ascc: AbstractSet[str], pri_max: int = PRI_ALL) -> List[str]:
    """Come up with the ideal processing order within an SCC.

    Using the priorities assigned by all_imported_modules_in_file(),
    try to reduce the cycle to a DAG, by omitting arcs representing
    dependencies of lower priority.

    In the simplest case, if we have A <--> B where A has a top-level
    "import B" (medium priority) but B only has the reverse "import A"
    inside a function (low priority), we turn the cycle into a DAG by
    dropping the B --> A arc, which leaves only A --> B.

    If all arcs have the same priority, we fall back to sorting by
    reverse global order (the order in which modules were first
    encountered).

    The algorithm is recursive, as follows: when as arcs of different
    priorities are present, drop all arcs of the lowest priority,
    identify SCCs in the resulting graph, and apply the algorithm to
    each SCC thus found.  The recursion is bounded because at each
    recursion the spread in priorities is (at least) one less.

    In practice there are only a few priority levels (less than a
    dozen) and in the worst case we just carry out the same algorithm
    for finding SCCs N times.  Thus the complexity is no worse than
    the complexity of the original SCC-finding algorithm -- see
    strongly_connected_components() below for a reference.
    """
    if len(ascc) == 1:
        return [s for s in ascc]
    pri_spread = set()
    for id in ascc:
        state = graph[id]
        for dep in state.dependencies:
            if dep in ascc:
                pri = state.priorities.get(dep, PRI_HIGH)
                if pri < pri_max:
                    pri_spread.add(pri)
    if len(pri_spread) == 1:
        # Filtered dependencies are uniform -- order by global order.
        return sorted(ascc, key=lambda id: -graph[id].order)
    pri_max = max(pri_spread)
    sccs = sorted_components(graph, ascc, pri_max)
    # The recursion is bounded by the len(pri_spread) check above.
    return [s for ss in sccs for s in order_ascc(graph, ss, pri_max)]


def process_fresh_modules(graph: Graph, modules: List[str], manager: BuildManager) -> None:
    """Process the modules in one group of modules from their cached data.

    This can be used to process an SCC of modules
    This involves loading the tree from JSON and then doing various cleanups.
    """
    for id in modules:
        graph[id].load_tree()
    for id in modules:
        graph[id].fix_cross_refs()
    for id in modules:
        graph[id].patch_dependency_parents()


def process_stale_scc(graph: Graph, scc: List[str], manager: BuildManager) -> None:
    """Process the modules in one SCC from source code.

    Exception: If quick_and_dirty is set, use the cache for fresh modules.
    """
    if manager.options.quick_and_dirty:
        fresh = [id for id in scc if graph[id].is_fresh()]
        fresh_set = set(fresh)  # To avoid running into O(N**2)
        stale = [id for id in scc if id not in fresh_set]
        if fresh:
            manager.log("  Fresh ids: %s" % (", ".join(fresh)))
        if stale:
            manager.log("  Stale ids: %s" % (", ".join(stale)))
    else:
        fresh = []
        stale = scc
    for id in fresh:
        graph[id].load_tree()
    for id in stale:
        # We may already have parsed the module, or not.
        # If the former, parse_file() is a no-op.
        graph[id].parse_file()
        graph[id].fix_suppressed_dependencies(graph)
    if 'typing' in scc:
        # For historical reasons we need to manually add typing aliases
        # for built-in generic collections, see docstring of
        # SemanticAnalyzerPass2.add_builtin_aliases for details.
        typing_mod = graph['typing'].tree
        assert typing_mod, "The typing module was not parsed"
        manager.semantic_analyzer.add_builtin_aliases(typing_mod)
    for id in fresh:
        graph[id].fix_cross_refs()
    for id in stale:
        graph[id].semantic_analysis()
    for id in stale:
        graph[id].semantic_analysis_pass_three()
    for id in stale:
        graph[id].semantic_analysis_apply_patches()
    for id in stale:
        graph[id].type_check_first_pass()
    more = True
    while more:
        more = False
        for id in stale:
            if graph[id].type_check_second_pass():
                more = True
    for id in stale:
        graph[id].generate_unused_ignore_notes()
    if any(manager.errors.is_errors_for_file(graph[id].xpath) for id in stale):
        for id in stale:
            graph[id].transitive_error = True
    for id in stale:
        graph[id].finish_passes()
        if manager.options.cache_fine_grained or manager.options.fine_grained_incremental:
            graph[id].compute_fine_grained_deps()
        manager.flush_errors(manager.errors.file_messages(graph[id].xpath), False)
        graph[id].write_cache()
        graph[id].mark_as_rechecked()


def sorted_components(graph: Graph,
                      vertices: Optional[AbstractSet[str]] = None,
                      pri_max: int = PRI_ALL) -> List[AbstractSet[str]]:
    """Return the graph's SCCs, topologically sorted by dependencies.

    The sort order is from leaves (nodes without dependencies) to
    roots (nodes on which no other nodes depend).

    This works for a subset of the full dependency graph too;
    dependencies that aren't present in graph.keys() are ignored.
    """
    # Compute SCCs.
    if vertices is None:
        vertices = set(graph)
    edges = {id: deps_filtered(graph, vertices, id, pri_max) for id in vertices}
    sccs = list(strongly_connected_components(vertices, edges))
    # Topsort.
    sccsmap = {id: frozenset(scc) for scc in sccs for id in scc}
    data = {}  # type: Dict[AbstractSet[str], Set[AbstractSet[str]]]
    for scc in sccs:
        deps = set()  # type: Set[AbstractSet[str]]
        for id in scc:
            deps.update(sccsmap[x] for x in deps_filtered(graph, vertices, id, pri_max))
        data[frozenset(scc)] = deps
    res = []
    for ready in topsort(data):
        # Sort the sets in ready by reversed smallest State.order.  Examples:
        #
        # - If ready is [{x}, {y}], x.order == 1, y.order == 2, we get
        #   [{y}, {x}].
        #
        # - If ready is [{a, b}, {c, d}], a.order == 1, b.order == 3,
        #   c.order == 2, d.order == 4, the sort keys become [1, 2]
        #   and the result is [{c, d}, {a, b}].
        res.extend(sorted(ready,
                          key=lambda scc: -min(graph[id].order for id in scc)))
    return res


def deps_filtered(graph: Graph, vertices: AbstractSet[str], id: str, pri_max: int) -> List[str]:
    """Filter dependencies for id with pri < pri_max."""
    if id not in vertices:
        return []
    state = graph[id]
    return [dep
            for dep in state.dependencies
            if dep in vertices and state.priorities.get(dep, PRI_HIGH) < pri_max]


def strongly_connected_components(vertices: AbstractSet[str],
                                  edges: Dict[str, List[str]]) -> Iterator[Set[str]]:
    """Compute Strongly Connected Components of a directed graph.

    Args:
      vertices: the labels for the vertices
      edges: for each vertex, gives the target vertices of its outgoing edges

    Returns:
      An iterator yielding strongly connected components, each
      represented as a set of vertices.  Each input vertex will occur
      exactly once; vertices not part of a SCC are returned as
      singleton sets.

    From http://code.activestate.com/recipes/578507/.
    """
    identified = set()  # type: Set[str]
    stack = []  # type: List[str]
    index = {}  # type: Dict[str, int]
    boundaries = []  # type: List[int]

    def dfs(v: str) -> Iterator[Set[str]]:
        index[v] = len(stack)
        stack.append(v)
        boundaries.append(index[v])

        for w in edges[v]:
            if w not in index:
                # For Python >= 3.3, replace with "yield from dfs(w)"
                for scc in dfs(w):
                    yield scc
            elif w not in identified:
                while index[w] < boundaries[-1]:
                    boundaries.pop()

        if boundaries[-1] == index[v]:
            boundaries.pop()
            scc = set(stack[index[v]:])
            del stack[index[v]:]
            identified.update(scc)
            yield scc

    for v in vertices:
        if v not in index:
            # For Python >= 3.3, replace with "yield from dfs(v)"
            for scc in dfs(v):
                yield scc


def topsort(data: Dict[AbstractSet[str],
                       Set[AbstractSet[str]]]) -> Iterable[Set[AbstractSet[str]]]:
    """Topological sort.

    Args:
      data: A map from SCCs (represented as frozen sets of strings) to
            sets of SCCs, its dependencies.  NOTE: This data structure
            is modified in place -- for normalization purposes,
            self-dependencies are removed and entries representing
            orphans are added.

    Returns:
      An iterator yielding sets of SCCs that have an equivalent
      ordering.  NOTE: The algorithm doesn't care about the internal
      structure of SCCs.

    Example:
      Suppose the input has the following structure:

        {A: {B, C}, B: {D}, C: {D}}

      This is normalized to:

        {A: {B, C}, B: {D}, C: {D}, D: {}}

      The algorithm will yield the following values:

        {D}
        {B, C}
        {A}

    From http://code.activestate.com/recipes/577413/.
    """
    # TODO: Use a faster algorithm?
    for k, v in data.items():
        v.discard(k)  # Ignore self dependencies.
    for item in set.union(*data.values()) - set(data.keys()):
        data[item] = set()
    while True:
        ready = {item for item, dep in data.items() if not dep}
        if not ready:
            break
        yield ready
        data = {item: (dep - ready)
                for item, dep in data.items()
                if item not in ready}
    assert not data, "A cyclic dependency exists amongst %r" % data
