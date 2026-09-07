"""Static HTTP/query/response inventory for the mobile contract gate (no app imports)."""
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
    for node in ast.parse(path.read_text()).body:
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
            arguments = node.args.args + node.args.kwonlyargs
            defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + node.args.defaults + node.args.kw_defaults
            query = []
            for argument, default in zip(arguments, defaults):
                annotation = name(argument.annotation)
                factory = name(default.func) if isinstance(default, ast.Call) else ""
                if argument.arg in path_names or factory in {"Depends", "Header", "Body", "Cookie", "Path"}:
                    continue
                if annotation in {"Request", "Response", "AsyncSession"} or annotation in schemas:
                    continue
                # Router dependencies and body models are explicitly excluded above.
                # Current query types are scalar/optional/Literal values.
                alias = literal(keyword(default, "alias")) if factory == "Query" else None
                query.append(alias or argument.arg)
            returns = keyword(decorator, "response_model") or node.returns
            routes.append({"method": decorator.func.attr.upper(), "path": route_path,
                           "query": query, "returns": name(returns), "file": path.name})

print(json.dumps({"routes": routes, "schemas": schemas, "events": events}, sort_keys=True))
