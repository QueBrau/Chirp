"""The single definition of the Pydantic base that every schema class here inherits.

Lives here because fifteen schema modules were each carrying their own byte-identical
copy of this two-line class — fifteen separate places for the config to drift apart.
Drop populate_by_name from one of them and only that domain's aliased request bodies
start 422-ing; drop from_attributes and only that domain's ORM responses break, at
runtime, in whichever router happened to be serialising the model. The re-export
barrel that used to sit in __init__.py drifted exactly that way before c238 deleted
it, and nothing caught it because nothing imported it.

The name stays `_Schema` so no schema class body had to change, and the leading
underscore still says package-internal: routers subclass the concrete schemas.
"""
from pydantic import BaseModel, ConfigDict


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
