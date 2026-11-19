"""Bearer-token auth for the SCIM routes. The IdP holds one token at a time
(``onyx/db/scim.py:create_scim_token__no_commit`` revokes older ones).

``onyx/server/auth_check.py`` accepts ``verify_scim_token`` as the auth
dependency of a route, through ``fetch_ee_implementation_or_noop``."""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from onyx.db.engine.sql_engine import get_session
from onyx.db.models import ScimToken
from onyx.db.scim import fetch_scim_token_by_raw, touch_scim_token__no_commit
from onyx.server.scim.models import ScimError


def verify_scim_token(
    request: Request, db_session: Session = Depends(get_session)
) -> ScimToken:
    header = request.headers.get("Authorization", "")
    scheme, _, raw_token = header.partition(" ")
    if scheme.lower() != "bearer" or not raw_token.strip():
        raise ScimError(401, "A SCIM bearer token is required.")
    token = fetch_scim_token_by_raw(db_session, raw_token.strip())
    if token is None:
        raise ScimError(401, "The SCIM token is invalid or revoked.")
    touch_scim_token__no_commit(db_session, token)
    db_session.commit()
    return token
