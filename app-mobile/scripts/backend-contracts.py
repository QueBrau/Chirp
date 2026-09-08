"""Static HTTP/query/response inventory for the mobile contract gate (no app imports).

Dependency queries are explicit Query markers on named, module-local functions,
including Annotated metadata and literal decorator dependency lists. Traversal
never imports auth/session dependencies, executes factories, infers helper scalar
parameters, or expands arbitrary dependency objects. Unsupported query sources
remain absent rather than granting wildcard query permission to a consumer.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def name(node):
    return ast.unparse(node) if node is not None else ""


def literal(node):
    return node.value if isinstance(node, ast.Constant) else None


def keyword(call, key):
    return next((part.value for part in call.keywords if part.arg == key), None)


PARAMETER_MARKERS = {"Query", "Depends", "Header", "Body", "Cookie", "Path"}


def parameters(function):
    positional = function.args.posonlyargs + function.args.args
    arguments = positional + function.args.kwonlyargs
    defaults = ([None] * (len(positional) - len(function.args.defaults))
                + function.args.defaults + function.args.kw_defaults)
    return zip(arguments, defaults)


def parameter_marker(argument, default):
    if isinstance(default, ast.Call):
        return default
    annotation = argument.annotation
    if (isinstance(annotation, ast.Subscript)
            and name(annotation.value) in {"Annotated", "typing.Annotated"}
            and isinstance(annotation.slice, ast.Tuple)):
        markers = [value for value in annotation.slice.elts[1:]
                   if isinstance(value, ast.Call) and name(value.func) in PARAMETER_MARKERS]
        if len(markers) > 1:
            raise RuntimeError("Ambiguous parameter markers")
        return markers[0] if markers else None
    return None


def query_name(argument, marker):
    alias_node = keyword(marker, "alias")
    if alias_node is None:
        return argument.arg
    alias = literal(alias_node)
    if not isinstance(alias_node, ast.Constant) or (alias is not None and not isinstance(alias, str)):
        raise RuntimeError("Unresolved query alias")
    return alias or argument.arg


def route_queries(function, decorator, local_functions, path_names):
    query = []
    seen_functions = set()

    def dependency(marker):
        target = marker.args[0] if marker.args else keyword(marker, "dependency")
        if not isinstance(target, ast.Name) or target.id not in local_functions:
            return  # Imported functions, factories and dependency objects stay out of scope.
        if target.id in seen_functions:
            return
        seen_functions.add(target.id)
        visit(local_functions[target.id], direct=False)

    def visit(node, *, direct):
        for argument, default in parameters(node):
            marker = parameter_marker(argument, default)
            factory = name(marker.func) if marker is not None else ""
            if factory == "Depends":
                dependency(marker)
                continue
            if argument.arg in path_names:
                continue
            if factory == "Query":
                query.append(query_name(argument, marker))
            elif direct:
                annotation = name(argument.annotation)
                if factory in {"Header", "Body", "Cookie", "Path"}:
                    continue
                if annotation in {"Request", "Response", "AsyncSession"} or annotation in schemas:
                    continue
                # Retain existing direct-route scalar inference; helpers require Query.
                query.append(argument.arg)

    visit(function, direct=True)
    dependencies = keyword(decorator, "dependencies")
    if isinstance(dependencies, (ast.List, ast.Tuple)):
        for marker in dependencies.elts:
            if isinstance(marker, ast.Call) and name(marker.func) == "Depends":
                dependency(marker)
    return list(dict.fromkeys(query))


schemas = {}
schema_files = sorted((ROOT / "backend/app/schemas").glob("*.py")) + sorted((ROOT / "backend/app/routers").glob("*.py"))
for path in schema_files:
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields = {}
        for field in node.body:
            if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
                continue
            field_name = field.target.id
            if field_name.startswith("_") or field_name == "model_config":
                continue
            if isinstance(field.value, ast.Call) and name(field.value.func) == "Field":
                alias = keyword(field.value, "serialization_alias") or keyword(field.value, "alias")
                field_name = literal(alias) or field_name
            fields[field_name] = name(field.annotation)
        if node.name in schemas:
            raise RuntimeError(f"Ambiguous schema name: {node.name}")
        schemas[node.name] = {"fields": fields, "bases": [name(base) for base in node.bases]}

routes = []
events = {}
for path in sorted((ROOT / "backend/app/routers").glob("*.py")):
    module = ast.parse(path.read_text())
    local_functions = {}
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in local_functions:
                raise RuntimeError(f"Ambiguous dependency function: {path.name}:{node.name}")
            local_functions[node.name] = node
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Current publishers construct literal event dictionaries. Inspect each
        # assignment, not arbitrary dictionaries or comments mentioning a type.
        for part in ast.walk(node):
            value = part.value if isinstance(part, (ast.Assign, ast.AnnAssign)) else None
            if not isinstance(value, ast.Dict):
                continue
            fields = {literal(key): value for key, value in zip(value.keys, value.values)}
            event_type = literal(fields.get("type"))
            if event_type in {"message", "poll"}:
                if any(not isinstance(key, str) for key in fields):
                    raise RuntimeError(f"Unresolved event keys: {path.name}:{part.lineno}")
                events.setdefault(event_type, []).append(sorted(fields))
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if name(decorator.func.value) != "router" or decorator.func.attr not in {"get", "post", "put", "patch", "delete"}:
                continue
            route_path = literal(decorator.args[0]) if decorator.args else literal(keyword(decorator, "path"))
            if not isinstance(route_path, str):
                raise RuntimeError(f"Unresolved backend route: {path.name}:{node.lineno}")
            path_names = set(re.findall(r"\{([^}:]+)(?::[^}]+)?\}", route_path))
            query = route_queries(node, decorator, local_functions, path_names)
            returns = keyword(decorator, "response_model") or node.returns
            routes.append({"method": decorator.func.attr.upper(), "path": route_path,
                           "query": query, "returns": name(returns), "file": path.name})

print(json.dumps({"routes": routes, "schemas": schemas, "events": events}, sort_keys=True))
