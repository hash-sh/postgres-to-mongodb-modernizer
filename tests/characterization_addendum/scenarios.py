"""Characterization ADDENDUM: behaviour found untested during review, recorded against the ORIGINAL app.

Added after Stage 1 (gap: no test changed a user's email). Recorded against the original app, then frozen in
tests/MANIFEST_addendum.sha256 BEFORE the converted code was fixed, so the fix is graded by the original's behaviour.
"""
from tests.characterization.scenarios import get, reg

SCENARIOS = [
    {"name": "email_change", "slice": 1, "steps": [
        reg("sam"),
        {"name": "change email (and bio)", "method": "PUT", "path": "/api/user", "as": "sam",
         "body": {"user": {"email": "sam.new@example.com", "bio": "moved", "password": "pw-sam"}}},
        {"name": "login with NEW email", "method": "POST", "path": "/api/users/login", "save_token": "sam2",
         "body": {"user": {"email": "sam.new@example.com", "password": "pw-sam"}}},
        get("current user via new-email token", "/api/user", "sam2"),
        {"name": "login with OLD email", "method": "POST", "path": "/api/users/login",
         "body": {"user": {"email": "sam@example.com", "password": "pw-sam"}}},
        get("profile after email change", "/api/profiles/sam"),
        {"name": "register again with the old email", "method": "POST", "path": "/api/users",
         "body": {"user": {"username": "sam_again", "email": "sam@example.com", "password": "x"}}},
    ]},
]
