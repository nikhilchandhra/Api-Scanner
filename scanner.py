import os, re, json, threading, webbrowser, time, sys, logging
import ipaddress, socket, urllib.parse, urllib.request, urllib.error
import subprocess
from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "scanner.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# ---------------------------------------------------------------------------
# Logger — log to scanner.log (mode 'w', utf-8) and stdout
# ---------------------------------------------------------------------------
logger = logging.getLogger("api_scanner")
logger.setLevel(logging.DEBUG)
logger.handlers.clear()
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.DEBUG)
_stdout_handler.setFormatter(_fmt)
logger.addHandler(_stdout_handler)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "port": 5000,
    "last_project": "",
    "recent_projects": [],
    "urls": {"LOCAL": "http://localhost:8080"},
    "project_overrides": {}
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config():
    """Load config.json, merge missing keys from DEFAULT_CONFIG, normalize shapes."""
    cfg = {}
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
    except Exception as e:
        logger.error("Failed to read config.json: %s", e)
        cfg = {}

    if not isinstance(cfg, dict):
        cfg = {}

    # Merge missing top-level keys from defaults.
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = json.loads(json.dumps(v))  # deep copy of default

    # Ensure urls is a dict containing LOCAL.
    if not isinstance(cfg.get("urls"), dict):
        cfg["urls"] = {"LOCAL": "http://localhost:8080"}
    if "LOCAL" not in cfg["urls"] or not isinstance(cfg["urls"]["LOCAL"], str):
        cfg["urls"]["LOCAL"] = "http://localhost:8080"

    # Ensure recent_projects is a list.
    if not isinstance(cfg.get("recent_projects"), list):
        cfg["recent_projects"] = []

    # Ensure project_overrides is a dict.
    if not isinstance(cfg.get("project_overrides"), dict):
        cfg["project_overrides"] = {}

    # Ensure port is an int.
    try:
        cfg["port"] = int(cfg.get("port", 5000))
    except (TypeError, ValueError):
        cfg["port"] = 5000

    if not isinstance(cfg.get("last_project"), str):
        cfg["last_project"] = ""

    return cfg


def save_config(cfg):
    """Write config as indented JSON."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        logger.error("Failed to write config.json: %s", e)


# ---------------------------------------------------------------------------
# Java parsing constants
# ---------------------------------------------------------------------------
PRIMITIVE_DEFAULTS = {
    "string": "string", "String": "string",
    "int": 0, "Integer": 0,
    "long": 0, "Long": 0,
    "double": 0.0, "Double": 0.0,
    "float": 0.0, "Float": 0.0,
    "boolean": True, "Boolean": True,
    "BigDecimal": "0.00",
    "LocalDate": "2024-01-15",
    "LocalDateTime": "2024-01-15T10:30:00",
    "Date": "2024-01-15",
    "UUID": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "Object": {},
    "byte": 0, "short": 0, "char": "a",
}

SKIP_DIRS = {'target', 'build', '.git', '.idea', 'node_modules', '.mvn', 'out', 'bin', '.gradle',
             'test-classes', 'generated-sources', 'generated-test-sources', 'maven-status',
             'surefire-reports', 'site', 'jacoco', 'test', 'tests', 'test-java', 'it',
             'integration-test', 'integration-tests'}

MODEL_DIR_NAMES = {'model', 'models', 'dto', 'dtos', 'domain', 'domains', 'response', 'responses',
                   'request', 'requests', 'firmrequest', 'decline', 'rectification', 'task', 'tasks',
                   'entity', 'entities', 'vo', 'payload', 'payloads', 'data', 'schedule', 'schedules',
                   'submission', 'submissions', 'history', 'detail', 'details',
                   'bean', 'beans', 'pojo', 'pojos', 'form', 'forms', 'credential', 'credentials',
                   'logincredentials', 'command', 'commands', 'resource', 'resources'}

CONTROLLER_DIR_NAMES = {'controller', 'controllers'}
SERVICE_DIR_NAMES = {'service', 'services'}


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------
def find_controller_and_model_files(folder):
    """Walk folder; collect controller, model and service .java files by directory name."""
    controller_files = []
    model_files = []
    service_files = []

    for root, dirs, files in os.walk(folder):
        # Prune skip dirs in-place.
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]

        base = os.path.basename(root).lower()
        java_files = [os.path.join(root, f) for f in files if f.endswith(".java")]

        if base in CONTROLLER_DIR_NAMES:
            controller_files.extend(java_files)
            dirs[:] = []  # no recursion below a controller dir
        elif base in SERVICE_DIR_NAMES:
            service_files.extend(java_files)
            # skip the 'impl' subdir
            dirs[:] = [d for d in dirs if d.lower() != "impl"]
        elif base in MODEL_DIR_NAMES:
            model_files.extend(java_files)

    logger.info("Found %d controller files, %d model files, %d service files",
                len(controller_files), len(model_files), len(service_files))
    return controller_files, model_files, service_files


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def read_file_safe(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        logger.error("Could not read %s: %s", path, e)
        return None


def extract_class_name(content):
    m = re.search(r'(?:public\s+)?(?:class|interface|enum)\s+(\w+)', content)
    return m.group(1) if m else None


def extract_package(content):
    m = re.search(r'^\s*package\s+([\w.]+)\s*;', content, re.MULTILINE)
    return m.group(1) if m else None


def is_controller(content):
    return "@RestController" in content or "@Controller" in content


def extract_class_base_path(content):
    """Extract the base path from a class-level @RequestMapping."""
    # @RequestMapping(value = {"/path"}) / @RequestMapping({"/path"}) / @RequestMapping("/path")
    m = re.search(r'@RequestMapping\s*\(([^)]*)\)', content)
    if not m:
        # bare @RequestMapping with no args
        if re.search(r'@RequestMapping\b', content):
            return ""
        return ""
    args = m.group(1)
    val = extract_annotations_value(args)
    if val == "/":
        return ""
    return val


def extract_annotations_value(annotation_str):
    """Extract the first quoted string from annotation args; '/' if nothing found."""
    if annotation_str is None:
        return "/"
    m = re.search(r'"([^"]*)"', annotation_str)
    if m:
        return m.group(1)
    return "/"


def extract_params_str(params_raw):
    """
    Depth-count starting at depth=1. Increment on '(', decrement on ')'.
    Stop when depth hits 0. Returns the parameter list contents (without the
    enclosing parens). This correctly handles annotation args containing ')'.
    """
    depth = 1
    collected = []
    for ch in params_raw:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                break
        collected.append(ch)
    return "".join(collected)


def split_params(params_str):
    """Split on ',' only when <> and () depth are both 0."""
    parts = []
    depth_angle = 0
    depth_paren = 0
    current = []
    for ch in params_str:
        if ch == '<':
            depth_angle += 1
        elif ch == '>':
            if depth_angle > 0:
                depth_angle -= 1
        elif ch == '(':
            depth_paren += 1
        elif ch == ')':
            if depth_paren > 0:
                depth_paren -= 1
        if ch == ',' and depth_angle == 0 and depth_paren == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p.strip()]


def extract_param_name_type(param_str, annotation):
    """
    Extract {"type", "name"} from a single parameter string.
    Honors explicit value=/name= in the annotation for the external name,
    but the Java identifier (type/name) comes from the declaration itself.
    """
    if not param_str or not param_str.strip():
        return None

    s = param_str.strip()

    # Determine explicit external name from annotation args (value= / name= / positional).
    explicit_name = None
    if annotation:
        ann_match = re.search(re.escape(annotation) + r'\s*\(([^)]*)\)', s)
        if ann_match:
            ann_args = ann_match.group(1)
            nm = re.search(r'\b(?:value|name)\s*=\s*"([^"]*)"', ann_args)
            if nm:
                explicit_name = nm.group(1)
            else:
                # positional first quoted string, but not defaultValue=
                pm = re.search(r'"([^"]*)"', ann_args)
                if pm and "defaultValue" not in ann_args[:pm.start()]:
                    explicit_name = pm.group(1)

    # Strip ALL annotations (with or without args) from the declaration.
    cleaned = re.sub(r'@\w+\s*(?:\([^)]*\))?', ' ', s).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    if not cleaned:
        return None

    parts = cleaned.split(' ')
    if len(parts) >= 2:
        result = {"type": parts[-2], "name": parts[-1]}
    else:
        result = {"type": "String", "name": parts[0]}

    if explicit_name:
        result["name"] = explicit_name
    return result


# Spring MVC infrastructure parameter types that are not request payloads.
_FRAMEWORK_PARAM_TYPES = {
    "Model", "ModelMap", "ModelAndView", "BindingResult", "Errors",
    "Principal", "HttpServletRequest", "HttpServletResponse", "HttpSession",
    "RedirectAttributes", "Locale", "SessionStatus", "UriComponentsBuilder",
    "MultipartFile", "WebRequest", "NativeWebRequest", "Pageable",
}


def _extract_object_param_type(param_str):
    """Return the Java type of a parameter, stripping all annotations and args."""
    if not param_str or not param_str.strip():
        return None
    cleaned = re.sub(r'@\w+\s*(?:\([^)]*\))?', ' ', param_str).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if not cleaned:
        return None
    parts = cleaned.split(' ')
    # Type is the second-to-last token (last is the variable name); if a single
    # token remains, that token is the type.
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def extract_method_body(content, start_pos):
    """Scan forward from start_pos (brace depth already at 1); collect until depth hits 0."""
    depth = 1
    collected = []
    i = start_pos
    n = len(content)
    while i < n:
        ch = content[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                break
        collected.append(ch)
        i += 1
    return "".join(collected)


# ---------------------------------------------------------------------------
# Controller method parser
# ---------------------------------------------------------------------------
_MAPPING_METHODS = {
    "@GetMapping": "GET",
    "@PostMapping": "POST",
    "@PutMapping": "PUT",
    "@DeleteMapping": "DELETE",
    "@PatchMapping": "PATCH",
}

_SIG_RE = re.compile(r'(?:public|protected|private)\s+([\w<>\[\],\s?]+?)\s+(\w+)\s*\((.*)$', re.DOTALL)


def _find_class_body_start(content):
    """Return index (char offset) right after the class-opening '{' (depth becomes 1)."""
    depth = 0
    for i, ch in enumerate(content):
        if ch == '{':
            depth += 1
            if depth == 1:
                return i + 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
    return None


def extract_methods_from_controller(content, base_path, controller_name, warnings):
    """Core controller parser. Returns a list of endpoint dicts."""
    endpoints = []

    body_start = _find_class_body_start(content)
    if body_start is None:
        warnings.append(f"Could not locate class body in {controller_name}")
        return endpoints

    lines = content.splitlines(keepends=True)

    # Map char offset -> line index, and starting offset of each line.
    line_offsets = []
    acc = 0
    for ln in lines:
        line_offsets.append(acc)
        acc += len(ln)

    # Find the line index where the class body begins.
    start_line = 0
    for idx, off in enumerate(line_offsets):
        if off >= body_start:
            start_line = idx
            break
    else:
        start_line = len(lines)

    i = start_line
    n_lines = len(lines)

    while i < n_lines:
        line = lines[i]
        stripped = line.strip()

        if not stripped.startswith('@'):
            i += 1
            continue

        # Collect a block of annotation lines (allow blank lines between).
        ann_lines = []
        j = i
        while j < n_lines:
            cur = lines[j].strip()
            if cur.startswith('@') or cur == '':
                ann_lines.append(lines[j])
                j += 1
            else:
                break

        ann_text = "".join(ann_lines)

        # Detect HTTP method.
        http_method = None
        for ann, verb in _MAPPING_METHODS.items():
            if ann in ann_text:
                http_method = verb
                break
        if http_method is None:
            rm = re.search(r'@RequestMapping\s*\(([^)]*)\)', ann_text, re.DOTALL)
            if rm and "RequestMethod." in rm.group(1):
                mm = re.search(r'RequestMethod\.(\w+)', rm.group(1))
                if mm:
                    http_method = mm.group(1).upper()
        if http_method is None:
            # Not a request-mapping annotation block; move past it.
            i = j
            continue

        # Extract path suffix from the mapping annotation.
        path_suffix = ""
        map_ann = re.search(
            r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(([^)]*)\)',
            ann_text, re.DOTALL)
        if map_ann:
            path_suffix = _extract_mapping_path(map_ann.group(1))

        # Collect signature lines until we find the opening '{' of the method.
        sig_parts = []
        brace_line = None
        k = j
        while k < n_lines:
            sig_parts.append(lines[k])
            if '{' in lines[k]:
                brace_line = k
                break
            if ';' in lines[k]:
                # abstract/interface-style method, no body — stop.
                brace_line = k
                break
            k += 1

        sig_text = "".join(sig_parts)
        sig_match = _SIG_RE.search(sig_text)
        if not sig_match:
            i = (brace_line + 1) if brace_line is not None else j + 1
            continue

        return_type = sig_match.group(1).strip()
        method_name = sig_match.group(2).strip()
        params_raw = sig_match.group(3)

        params_str = extract_params_str(params_raw)
        param_list = split_params(params_str)

        path_vars = []
        query_params = []
        request_body_type = None
        request_headers = []

        for p in param_list:
            if '@PathVariable' in p:
                info = extract_param_name_type(p, '@PathVariable')
                if info:
                    path_vars.append(info)
            elif '@RequestParam' in p:
                info = extract_param_name_type(p, '@RequestParam')
                if info:
                    query_params.append(info)
            elif '@RequestBody' in p:
                body_clean = re.sub(r'@\w+\s*(?:\([^)]*\))?', ' ', p).strip()
                body_clean = re.sub(r'\s+', ' ', body_clean)
                bparts = body_clean.split(' ')
                if len(bparts) >= 1 and bparts[0]:
                    request_body_type = bparts[0]
            elif '@ModelAttribute' in p:
                # Spring MVC form binding — treat the bound object as the request body.
                mtype = _extract_object_param_type(p)
                if mtype and mtype not in _FRAMEWORK_PARAM_TYPES:
                    request_body_type = mtype
            elif '@RequestHeader' in p:
                # skip Map<...> catch-all header params
                if re.search(r'\bMap\s*<', p):
                    continue
                info = extract_param_name_type(p, '@RequestHeader')
                if info:
                    request_headers.append(info)

        # Build the full path.
        bp = base_path.strip('/')
        ps = path_suffix.strip('/')
        segs = [s for s in (bp, ps) if s]
        full_path = '/' + '/'.join(segs)
        full_path = re.sub(r'/{2,}', '/', full_path)
        if full_path == '':
            full_path = '/'

        # Method body (for return-type inference & description).
        method_body = ""
        if brace_line is not None and '{' in lines[brace_line]:
            brace_offset = line_offsets[brace_line] + lines[brace_line].index('{')
            method_body = extract_method_body(content, brace_offset + 1)

        return_types = extract_return_types(method_body, return_type)
        description = generate_api_description(
            http_method, full_path, method_name, path_vars,
            query_params, request_body_type, method_body)

        endpoints.append({
            "method": http_method,
            "path": full_path,
            "method_name": method_name,
            "controller": controller_name,
            "path_vars": path_vars,
            "query_params": query_params,
            "request_body_type": request_body_type,
            "request_headers": request_headers,
            "return_type": return_type,
            "return_types": return_types,
            "description": description,
        })

        # Advance past this method.
        if brace_line is not None:
            i = brace_line + 1
        else:
            i = k + 1

    return endpoints


def _extract_mapping_path(args):
    """Extract path from mapping annotation args; '' if none/only method specified."""
    if args is None:
        return ""
    # Prefer value=/path= quoted strings.
    m = re.search(r'\b(?:value|path)\s*=\s*\{?\s*"([^"]*)"', args)
    if m:
        return m.group(1)
    # Positional first quoted string (array or single), ignoring method= etc.
    m = re.search(r'"([^"]*)"', args)
    if m:
        return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Description generator
# ---------------------------------------------------------------------------
VERB_MAP = {
    'get': 'Gets', 'fetch': 'Fetches', 'find': 'Finds', 'retrieve': 'Retrieves',
    'load': 'Loads', 'read': 'Reads', 'list': 'Lists', 'search': 'Searches',
    'download': 'Downloads', 'export': 'Exports', 'generate': 'Generates',
    'insert': 'Inserts', 'create': 'Creates', 'add': 'Adds', 'save': 'Saves',
    'submit': 'Submits', 'upload': 'Uploads', 'send': 'Sends', 'post': 'Posts',
    'update': 'Updates', 'edit': 'Edits', 'modify': 'Modifies', 'set': 'Sets',
    'patch': 'Patches', 'replace': 'Replaces',
    'delete': 'Deletes', 'remove': 'Removes', 'purge': 'Purges',
    'process': 'Processes', 'validate': 'Validates', 'check': 'Checks',
    'calculate': 'Calculates', 'compute': 'Computes', 'build': 'Builds',
}


def generate_api_description(http_method, path, method_name, path_vars,
                             query_params, request_body_type, method_body):
    # 1. First service-ish call in the body, else the method name.
    base_name = method_name
    if method_body:
        call = re.search(r'\w+\.\s*([a-z]\w+)\s*\(', method_body)
        if call:
            base_name = call.group(1)

    # 2. camelCase -> words.
    words = re.sub(r'([A-Z])', r' \1', base_name).strip().lower()
    word_tokens = words.split()

    # 3. Map the leading verb.
    if word_tokens:
        lead = word_tokens[0]
        if lead in VERB_MAP:
            verb = VERB_MAP[lead]
            rest = " ".join(word_tokens[1:]).strip()
            desc = (verb + (" " + rest if rest else "")).strip()
        else:
            desc = words
    else:
        desc = words

    if not desc:
        desc = f"{http_method} {path}"

    # 4. Append qualifiers.
    if path_vars:
        names = ",".join(pv["name"] for pv in path_vars)
        desc += f" identified by {names}"
    if request_body_type:
        desc += f" using the provided {request_body_type}"
    if query_params:
        names = ",".join(qp["name"] for qp in query_params)
        desc += f" with parameters: {names}"

    return desc.strip()


# ---------------------------------------------------------------------------
# Service interface cache
# ---------------------------------------------------------------------------
_service_method_cache = {}

# Interface-style method signatures (end with ';').
_SERVICE_SIG_RE = re.compile(
    r'^\s*([\w<>\[\],\s]+?)\s+(\w+)\s*\([^\)]*\)\s*(?:throws [^;]+)?;',
    re.MULTILINE)

# Concrete-class public method signatures (body opens with '{').
# e.g. "public List<Product> getAllProducts() {" or "public Product getProduct(int id) {"
_SERVICE_IMPL_SIG_RE = re.compile(
    r'^\s*public\s+((?:[\w.]+(?:<[^>]*>)?(?:\[\])?))\s+(\w+)\s*\([^)]*\)\s*'
    r'(?:throws [\w.,\s]+)?\{',
    re.MULTILINE)

_NON_TYPE_KEYWORDS = {"public", "private", "protected", "static", "default", "void",
                      "final", "abstract", "synchronized", "class", "interface"}


def _store_service_method(name, ret_type):
    """Record a service method's return type, skipping non-types and void."""
    ret_tokens = ret_type.split()
    rt = ret_tokens[-1] if ret_tokens else ret_type
    if not rt or rt in _NON_TYPE_KEYWORDS:
        return
    _service_method_cache[name] = rt


def build_service_cache(service_files):
    """
    Build method_name -> return-type map from service files.

    Handles BOTH interface declarations (methods ending ';') AND concrete
    @Service/@Component classes (public methods with a '{' body). Many projects
    use concrete service classes rather than interfaces, so limiting to
    interfaces alone leaves return-type inference with nothing to work from.
    """
    global _service_method_cache
    _service_method_cache = {}
    for path in service_files:
        content = read_file_safe(path)
        if not content:
            continue
        if re.search(r'\binterface\b', content):
            for m in _SERVICE_SIG_RE.finditer(content):
                _store_service_method(m.group(2).strip(), m.group(1).strip())
        else:
            # Concrete class: scan public method signatures with bodies.
            for m in _SERVICE_IMPL_SIG_RE.finditer(content):
                _store_service_method(m.group(2).strip(), m.group(1).strip())
    logger.info("Service cache built with %d methods", len(_service_method_cache))


# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------
_model_cache = {}

# NOTE (bug fix): the spec's generic sub-pattern `<[=;]+>` cannot match real
# generics like `<String>`. Corrected to `<[^>]*>` so generic field types parse.
_FIELD_LINE_RE = re.compile(
    r'^\s*(?:@\w+(?:\([^)]*\))?\s*)*'
    r'(?:(?:private|protected|public)\s+)?'
    r'(?:(?:static|final|transient|volatile)\s+)*'
    r'((?:[\w]+(?:<[^>]*>)?(?:\[\])?))'
    r'\s+(\w+)\s*(?:=.*)?;\s*$'
)

_SKIP_FIELD_NAMES = {"serialVersionUID", "log", "logger", "LOGGER", "instance"}

# Java keywords / statement starters that must never be treated as a field TYPE.
# Without this, lines inside getter bodies like "return pid;" are mis-parsed as
# a field named "pid" of type "return", clobbering the real declaration.
_NON_TYPE_WORDS = {
    "return", "this", "new", "if", "else", "for", "while", "do", "switch",
    "case", "break", "continue", "throw", "throws", "try", "catch", "finally",
    "assert", "yield", "super", "package", "import", "class", "interface",
    "enum", "extends", "implements", "void", "public", "private", "protected",
    "static", "final", "abstract", "synchronized", "native", "transient",
    "volatile", "default", "instanceof",
}


def extract_enum_values(content):
    """Find enum body, take the part before the first ';', extract CONSTANT tokens."""
    brace = content.find('{')
    if brace == -1:
        return []
    body = extract_method_body(content, brace + 1)
    head = body.split(';', 1)[0]
    values = re.findall(r'\b([A-Z][A-Z0-9_]*)\b', head)
    # De-dup while preserving order.
    seen = set()
    result = []
    for v in values:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def extract_fields(content):
    """
    Parse class fields into {field_name: type}.

    Only considers declarations at class-body depth (depth 1); lines inside
    method/constructor bodies (depth >= 2) are skipped so that statements like
    'return pid;' are never mistaken for field declarations. Keyword "types"
    are rejected, and the first declaration of a name wins.
    """
    fields = {}
    body_start = _find_class_body_start(content)
    if body_start is None:
        return fields

    # Walk the class body, tracking brace depth so we only read fields that
    # sit directly in the class (depth 1), not inside method bodies.
    region = content[body_start:]
    depth = 1  # we are already inside the class body
    for line in region.splitlines():
        stripped = line.strip()

        # Only attempt a field match when at class-body depth and the line is
        # a simple declaration (not a block opener).
        if depth == 1 and '{' not in line and '}' not in line:
            m = _FIELD_LINE_RE.match(line)
            if m:
                ftype = m.group(1).strip()
                fname = m.group(2).strip()
                if (fname not in _SKIP_FIELD_NAMES
                        and not fname.isupper()
                        and '(' not in ftype and ')' not in ftype
                        and ftype not in _NON_TYPE_WORDS
                        and fname not in fields):
                    fields[fname] = ftype

        # Update depth after processing the line.
        depth += line.count('{') - line.count('}')
        if depth <= 0:
            break

    return fields


def build_model_cache(model_files):
    global _model_cache
    _model_cache = {}
    for path in model_files:
        content = read_file_safe(path)
        if not content:
            continue

        with _scan_lock:
            _scan_progress["current"] = os.path.basename(path)
            _scan_progress["done"] += 1

        cls = extract_class_name(content)
        if not cls:
            continue

        if re.search(r'\benum\b', content):
            _model_cache[cls] = {"__enum__": extract_enum_values(content)}
        else:
            _model_cache[cls] = extract_fields(content)
    logger.info("Model cache built with %d classes", len(_model_cache))


# ---------------------------------------------------------------------------
# Example JSON builder
# ---------------------------------------------------------------------------
_COLLECTION_RE = re.compile(r'^(?:List|Set|Collection|ArrayList|Iterable)\s*<(.+)>$')
_PAGE_RE = re.compile(r'^Page\s*<(.+)>$')
_MAP_RE = re.compile(r'^Map\s*<(.+)>$')


def default_for(type_name):
    """Primitive default for a type name, else a placeholder string."""
    if type_name in PRIMITIVE_DEFAULTS:
        val = PRIMITIVE_DEFAULTS[type_name]
        # Return copies for mutable defaults.
        if isinstance(val, dict):
            return {}
        return val
    return f"<{type_name}>"


def _strip_type(type_name):
    return (type_name or "").strip()


def build_example_json(type_name, depth=0, visited=None):
    if visited is None:
        visited = set()
    if depth > 5:
        return f"<{type_name}>"

    t = _strip_type(type_name)
    if not t:
        return {}

    # 1. List/Set/Collection/ArrayList<X>
    m = _COLLECTION_RE.match(t)
    if m:
        inner = m.group(1).strip()
        return [build_example_json(inner, depth + 1, visited)]

    # 2. Page<X>
    m = _PAGE_RE.match(t)
    if m:
        inner = m.group(1).strip()
        return [build_example_json(inner, depth + 1, visited)]

    # 3. Map<...>
    if _MAP_RE.match(t):
        return {"key": "value"}

    # 4. Primitive
    if t in PRIMITIVE_DEFAULTS:
        return default_for(t)

    # 5. Enum known in model cache
    if t in _model_cache and isinstance(_model_cache[t], dict) and "__enum__" in _model_cache[t]:
        vals = _model_cache[t]["__enum__"]
        return vals[0] if vals else "ENUM_VALUE"

    # 6. Known class in model cache (and not currently being visited)
    if t in _model_cache and t not in visited:
        fields = _model_cache[t]
        obj = {}
        new_visited = visited | {t}
        for fname, ftype in fields.items():
            if fname == "__enum__":
                continue
            obj[fname] = build_example_json(ftype, depth + 1, new_visited)
        return obj

    # 7. Unknown
    return f"<{t}>"


# ---------------------------------------------------------------------------
# Return-type inference
# ---------------------------------------------------------------------------
_UNWRAP_RE = re.compile(r'^(?:List|Set|Collection|ArrayList|Page|Iterable)\s*<(.+)>$')
_RESPONSE_ENTITY_RE = re.compile(r'^ResponseEntity\s*<(.+)>$')

_SKIP_RETURN_TOKENS = {"null", "true", "false", "this", "super"}


def _unwrap_collection(type_name):
    t = _strip_type(type_name)
    m = _UNWRAP_RE.match(t)
    if m:
        return m.group(1).strip()
    return t


def extract_return_types(method_body, declared_return):
    """Return a list of concrete type-name strings referenced by the method body."""
    results = []
    seen = set()

    def add(tp):
        if not tp:
            return
        tp = tp.strip()
        if not tp or tp in seen:
            return
        if tp in _SKIP_RETURN_TOKENS:
            return
        if not tp[0].isupper():
            return
        if tp in ("Object", "Void", "void"):
            return
        seen.add(tp)
        results.append(tp)

    body = method_body or ""

    # Pattern 0: service cache — scan ALL .method( calls.
    for call in re.finditer(r'\.\s*([a-zA-Z_]\w*)\s*\(', body):
        name = call.group(1)
        if name in _service_method_cache:
            ret = _service_method_cache[name]
            inner = _unwrap_collection(ret)
            if inner not in ("Object", "Void", "void"):
                add(inner)

    # Pattern 1: new ResponseEntity<>(new ClassName(
    for m in re.finditer(r'new\s+ResponseEntity\s*<[^>]*>\s*\(\s*new\s+(\w+)\s*\(', body):
        add(m.group(1))

    # Pattern 2: new ResponseEntity<>(variable, HttpStatus ...)
    for m in re.finditer(r'new\s+ResponseEntity\s*<[^>]*>\s*\(\s*(\w+)\s*,\s*HttpStatus', body):
        var = m.group(1)
        add(_find_var_type(body, var))

    # Pattern 3: ResponseEntity.ok/created/accepted(new ClassName(
    for m in re.finditer(r'ResponseEntity\.\s*(?:ok|created|accepted)\s*\(\s*new\s+(\w+)\s*\(', body):
        add(m.group(1))

    # Pattern 4: ResponseEntity.ok/created/accepted(variable)
    for m in re.finditer(r'ResponseEntity\.\s*(?:ok|created|accepted)\s*\(\s*(\w+)\s*\)', body):
        add(_find_var_type(body, m.group(1)))

    # Pattern 5: .body(new ClassName(
    for m in re.finditer(r'\.\s*body\s*\(\s*new\s+(\w+)\s*\(', body):
        add(m.group(1))

    # Pattern 6: return new ClassName( — skip ResponseEntity
    for m in re.finditer(r'return\s+new\s+(\w+)\s*[(<]', body):
        if m.group(1) != "ResponseEntity":
            add(m.group(1))

    # Fallback: unwrap ResponseEntity<X> from declared return.
    if not results:
        dr = _strip_type(declared_return)
        rem = _RESPONSE_ENTITY_RE.match(dr)
        if rem:
            inner = _unwrap_collection(rem.group(1).strip())
            add(inner)
        else:
            add(_unwrap_collection(dr))

    # Last resort: scan service cache once more, else Object.
    if not results:
        for call in re.finditer(r'\.\s*([a-zA-Z_]\w*)\s*\(', body):
            name = call.group(1)
            if name in _service_method_cache:
                add(_unwrap_collection(_service_method_cache[name]))
    if not results:
        return ['Object']

    return results


def _find_var_type(body, var):
    """Find 'TypeName var = ...' declaration and return TypeName."""
    m = re.search(r'\b([A-Z]\w*(?:<[^>]*>)?)\s+' + re.escape(var) + r'\s*=', body)
    if m:
        return _unwrap_collection(m.group(1).strip())
    return None


# ---------------------------------------------------------------------------
# Scan state
# ---------------------------------------------------------------------------
_scan_results = []
_scan_warnings = []
_scan_progress = {"current": "", "total": 0, "done": 0, "scanning": False}
_scan_lock = threading.Lock()


def scan_project(project_folder):
    global _scan_results, _scan_warnings, _scan_progress

    with _scan_lock:
        _scan_results = []
        _scan_warnings = []
        _scan_progress = {"current": "", "total": 0, "done": 0, "scanning": True}

    try:
        controller_files, model_files, service_files = find_controller_and_model_files(project_folder)

        if not controller_files:
            with _scan_lock:
                _scan_warnings.append("No controller files found in the selected project.")
                _scan_progress["scanning"] = False
            return []

        total = len(controller_files) + len(model_files)
        with _scan_lock:
            _scan_progress["total"] = total

        build_model_cache(model_files)
        build_service_cache(service_files)

        results = []
        warnings = []

        for cfile in controller_files:
            with _scan_lock:
                _scan_progress["current"] = os.path.basename(cfile)
                _scan_progress["done"] += 1

            content = read_file_safe(cfile)
            if not content:
                continue
            if not is_controller(content):
                continue

            controller_name = extract_class_name(content) or os.path.basename(cfile)
            base_path = extract_class_base_path(content)
            endpoints = extract_methods_from_controller(content, base_path, controller_name, warnings)

            if not endpoints:
                continue

            api_list = []
            for ep in endpoints:
                # Request example.
                if ep["request_body_type"]:
                    req_obj = build_example_json(ep["request_body_type"])
                    request_example = json.dumps(req_obj, indent=2)
                else:
                    request_example = None

                # Response examples.
                response_examples = []
                rtypes = ep.get("return_types") or []
                if len(rtypes) == 1:
                    ex = build_example_json(rtypes[0])
                    response_examples.append({
                        "label": "Response",
                        "example": json.dumps(ex, indent=2)
                    })
                else:
                    for rt in rtypes:
                        ex = build_example_json(rt)
                        response_examples.append({
                            "label": rt,
                            "example": json.dumps(ex, indent=2)
                        })

                api = dict(ep)
                api["request_example"] = request_example
                api["response_examples"] = response_examples
                api_list.append(api)

            results.append({
                "name": controller_name,
                "apis": api_list,
            })

        with _scan_lock:
            _scan_results = results
            _scan_warnings = warnings
            _scan_progress["scanning"] = False

        logger.info("Scan complete: %d controllers", len(results))
        return results

    except Exception as e:
        logger.exception("Scan failed: %s", e)
        with _scan_lock:
            _scan_warnings.append(f"Scan error: {e}")
            _scan_progress["scanning"] = False
        return []


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------
def _is_safe_folder(folder):
    """Block scanning of sensitive system directories."""
    try:
        real = os.path.realpath(folder)
    except Exception:
        return False

    real_norm = real.replace("\\", "/").lower()

    blocked = []
    for env_key in ("WINDIR", "SystemRoot"):
        val = os.environ.get(env_key)
        if val:
            blocked.append(os.path.realpath(val).replace("\\", "/").lower())
    blocked.extend(["/etc", "/bin", "/sbin", "/usr/bin", "/proc", "/sys"])

    for b in blocked:
        if not b:
            continue
        if real_norm == b or real_norm.startswith(b + "/"):
            return False
    return True


def _is_safe_url(url):
    """SSRF guard: allow localhost/127.0.0.1/::1, block other private/reserved IPs."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.hostname
    if not host:
        return False

    host_lower = host.lower()
    if host_lower in ("localhost", "127.0.0.1", "::1"):
        return True

    # Resolve all addresses for the host.
    addrs = set()
    try:
        info = socket.getaddrinfo(host, None)
        for item in info:
            addrs.add(item[4][0])
    except Exception:
        # Could be a literal IP.
        addrs.add(host)

    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return False
        if ip.is_loopback:
            continue
        if ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", config=load_config())


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def api_post_config():
    cfg = load_config()
    data = request.json or {}

    if "urls" in data and isinstance(data["urls"], dict):
        merged = dict(cfg.get("urls", {}))
        merged.update(data["urls"])
        if "LOCAL" not in merged:
            merged["LOCAL"] = cfg["urls"].get("LOCAL", "http://localhost:8080")
        cfg["urls"] = merged
    if "port" in data:
        try:
            cfg["port"] = int(data["port"])
        except (TypeError, ValueError):
            pass
    if "last_project" in data and isinstance(data["last_project"], str):
        cfg["last_project"] = data["last_project"]
    if "recent_projects" in data and isinstance(data["recent_projects"], list):
        cfg["recent_projects"] = data["recent_projects"][:10]

    save_config(cfg)
    return jsonify({"status": "ok"})


@app.route("/api/config/override", methods=["POST"])
def api_config_override():
    cfg = load_config()
    data = request.json or {}
    project = data.get("project")
    key = data.get("key")
    payload = data.get("payload", None)

    if not project or not key:
        return jsonify({"status": "error", "message": "project and key required"}), 400

    overrides = cfg.setdefault("project_overrides", {})
    proj_map = overrides.setdefault(project, {})

    if payload is None:
        if key in proj_map:
            del proj_map[key]
    else:
        proj_map[key] = payload

    save_config(cfg)
    return jsonify({"status": "ok"})


@app.route("/api/config/env/<env_name>", methods=["DELETE"])
def api_delete_env(env_name):
    if env_name == "LOCAL":
        return jsonify({"status": "error", "message": "Cannot remove LOCAL"}), 400
    cfg = load_config()
    if env_name in cfg.get("urls", {}):
        del cfg["urls"][env_name]
        save_config(cfg)
    return jsonify({"status": "ok"})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.json or {}
    folder = data.get("folder", "")

    if not folder or not os.path.isdir(folder):
        return jsonify({"status": "error", "message": "Folder does not exist"}), 400
    if not _is_safe_folder(folder):
        return jsonify({"status": "error", "message": "Folder path is not allowed"}), 400

    cfg = load_config()
    cfg["last_project"] = folder
    recent = cfg.get("recent_projects", [])
    recent = [p for p in recent if p != folder]
    recent.insert(0, folder)
    cfg["recent_projects"] = recent[:10]
    save_config(cfg)

    t = threading.Thread(target=scan_project, args=(folder,), daemon=True)
    t.start()

    return jsonify({"status": "scanning"})


@app.route("/api/scan/progress", methods=["GET"])
def api_scan_progress():
    with _scan_lock:
        return jsonify(dict(_scan_progress))


@app.route("/api/scan/results", methods=["GET"])
def api_scan_results():
    with _scan_lock:
        controllers = list(_scan_results)
        warnings = list(_scan_warnings)
    total_apis = sum(len(c.get("apis", [])) for c in controllers)
    return jsonify({
        "controllers": controllers,
        "total_controllers": len(controllers),
        "total_apis": total_apis,
        "warnings": warnings,
    })


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    def _shutdown():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_shutdown, daemon=False).start()
    return jsonify({"status": "shutting down"})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    data = request.json or {}
    cfg = load_config()
    new_port = cfg.get("port", 5000)
    if "port" in data:
        try:
            new_port = int(data["port"])
            cfg["port"] = new_port
            save_config(cfg)
        except (TypeError, ValueError):
            pass

    def _restart():
        time.sleep(1)
        try:
            subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0])])
        except Exception as e:
            logger.error("Restart failed: %s", e)
        os._exit(0)

    threading.Thread(target=_restart, daemon=False).start()
    return jsonify({"status": "restarting", "port": new_port})


@app.route("/api/browse-folder", methods=["GET"])
def api_browse_folder():
    path = ""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.wm_attributes('-topmost', True)
        path = filedialog.askdirectory()
        root.destroy()
    except Exception as e:
        logger.error("Folder browse failed: %s", e)
        path = ""
    return jsonify({"folder": path or ""})


@app.route("/api-detail")
def api_detail():
    try:
        ci = int(request.args.get("c", -1))
        ai = int(request.args.get("a", -1))
    except (TypeError, ValueError):
        return "Invalid parameters", 400

    with _scan_lock:
        controllers = list(_scan_results)

    if ci < 0 or ci >= len(controllers):
        return "Controller index out of range", 404
    apis = controllers[ci].get("apis", [])
    if ai < 0 or ai >= len(apis):
        return "API index out of range", 404

    return render_template("api_detail.html", api=apis[ai], config=load_config())


@app.route("/api/proxy", methods=["POST"])
def api_proxy():
    data = request.json or {}
    url = data.get("url", "")
    method = (data.get("method") or "GET").upper()
    headers = data.get("headers") or {}
    body = data.get("body")

    if not _is_safe_url(url):
        return jsonify({"status": 0, "statusText": "Blocked",
                        "body": "Target URL is not allowed (SSRF protection).",
                        "headers": {}, "elapsed": 0}), 400

    data_bytes = None
    if body is not None and method in ("POST", "PUT", "PATCH", "DELETE"):
        if isinstance(body, (dict, list)):
            data_bytes = json.dumps(body).encode("utf-8")
        else:
            data_bytes = str(body).encode("utf-8")

    req = urllib.request.Request(url, data=data_bytes, method=method)
    for k, v in headers.items():
        if k and v is not None:
            req.add_header(str(k), str(v))

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=62) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            resp_headers = {k: v for k, v in resp.headers.items()}
            elapsed = int((time.time() - start) * 1000)
            return jsonify({
                "status": resp.status,
                "statusText": getattr(resp, "reason", "") or "",
                "body": resp_body,
                "headers": resp_headers,
                "elapsed": elapsed,
            })
    except urllib.error.HTTPError as e:
        elapsed = int((time.time() - start) * 1000)
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        err_headers = {}
        try:
            err_headers = {k: v for k, v in e.headers.items()}
        except Exception:
            pass
        return jsonify({
            "status": e.code,
            "statusText": e.reason if isinstance(e.reason, str) else str(e.reason),
            "body": err_body,
            "headers": err_headers,
            "elapsed": elapsed,
        })
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        return jsonify({
            "status": 502,
            "statusText": "Bad Gateway",
            "body": f"Request failed: {e}",
            "headers": {},
            "elapsed": elapsed,
        }), 502


@app.route("/api/export-postman", methods=["POST"])
def api_export_postman():
    data = request.json or {}
    base_url = data.get("base_url", "http://localhost:8080")
    collection_name = data.get("collection_name", "API Collection")

    with _scan_lock:
        controllers = list(_scan_results)

    parsed = urllib.parse.urlparse(base_url)
    protocol = parsed.scheme or "http"
    host_str = parsed.hostname or "localhost"
    port = parsed.port

    items = []
    for ctrl in controllers:
        sub_items = []
        for api in ctrl.get("apis", []):
            raw_path = api.get("path", "/")

            # Replace {varName} with the default value for its type.
            resolved_path = raw_path
            for pv in api.get("path_vars", []):
                default_val = default_for(pv.get("type", "String"))
                resolved_path = resolved_path.replace(
                    "{" + pv.get("name", "") + "}", str(default_val))

            path_segments = [s for s in resolved_path.split("/") if s != ""]

            query_arr = []
            for qp in api.get("query_params", []):
                query_arr.append({
                    "key": qp.get("name", ""),
                    "value": str(default_for(qp.get("type", "String"))),
                    "description": qp.get("type", ""),
                    "disabled": False,
                })

            raw_url = base_url.rstrip("/") + resolved_path
            if query_arr:
                qstr = "&".join(f"{q['key']}={q['value']}" for q in query_arr)
                raw_url = raw_url + "?" + qstr

            url_obj = {
                "raw": raw_url,
                "protocol": protocol,
                "host": host_str.split("."),
                "path": path_segments,
            }
            if port:
                url_obj["port"] = str(port)
            if query_arr:
                url_obj["query"] = query_arr

            header_arr = []
            req_body_obj = None
            if api.get("request_example") is not None:
                header_arr.append({"key": "Content-Type", "value": "application/json"})
                req_body_obj = {
                    "mode": "raw",
                    "raw": api["request_example"],
                    "options": {"raw": {"language": "json"}},
                }

            request_obj = {
                "method": api.get("method", "GET"),
                "header": header_arr,
                "url": url_obj,
            }
            if req_body_obj:
                request_obj["body"] = req_body_obj

            sub_items.append({
                "name": f'{api.get("method", "GET")} {raw_path}',
                "request": request_obj,
            })

        items.append({"name": ctrl.get("name", "Controller"), "item": sub_items})

    collection = {
        "info": {
            "name": collection_name,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
    }

    safe_name = re.sub(r'[^\w\-]+', '_', collection_name).strip('_') or "collection"
    return jsonify({
        "collection": json.dumps(collection, indent=2),
        "collection_filename": f"{safe_name}.postman_collection.json",
    })


@app.route("/api/generate-readme", methods=["POST"])
def api_generate_readme():
    data = request.json or {}
    api = data.get("api") or {}
    live_test = data.get("live_test")
    readme_url = data.get("readme_url", "")
    cfg = load_config()

    html = render_template(
        "readme_template.html",
        api=api,
        urls=cfg.get("urls", {}),
        live_test=live_test,
        readme_url=readme_url,
    )
    method_name = api.get("method_name", "api")
    filename = f"{method_name}_README.html"
    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def open_browser(port):
    time.sleep(1.2)
    try:
        webbrowser.open(f"http://localhost:{port}")
    except Exception as e:
        logger.error("Could not open browser: %s", e)


if __name__ == '__main__':
    cfg = load_config()
    port = cfg.get('port', 5000)
    logger.info("Starting API Scanner on port %s", port)
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    app.run(port=port, debug=False)
