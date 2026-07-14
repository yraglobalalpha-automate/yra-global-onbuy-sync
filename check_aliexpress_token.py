"""Diagnose ALI_ACCESS_TOKEN problems in one run.

Makes a real aliexpress.ds.product.get call with your token sent both ways
the gateway is known to accept it ("access_token" and "session"), reports
which one works, and checks the three values for copy-paste damage
(stray spaces/line breaks). Run it locally:

  python check_aliexpress_token.py

It asks for the app key, app secret and access token when it starts -
paste each one and press Enter. Use the SAME values you saved as GitHub
secrets. NEVER type real values into this file itself - the file is in
the public repo, so anything written here gets published. Secrets are
never printed in full - only length and the first/last few characters.
"""
import json
import os
import sys
import time

import requests

from aliexpress_client import GATEWAY, sign_params

TEST_PRODUCT_ID = "1005007315593877"


def peek(value):
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return f"len={len(value)}"
    return f"len={len(value)}, {value[:4]}...{value[-4:]}"


def hygiene(name, value):
    problems = []
    if value != value.strip():
        problems.append("has leading/trailing whitespace")
    if any(ch in value for ch in "\r\n\t"):
        problems.append("contains a line break or tab (broken copy-paste)")
    if any(ch in value for ch in " "):
        problems.append("contains a space")
    if not value.isascii():
        problems.append("contains non-ASCII characters (smart quotes? copied from a rich-text app?)")
    status = "; ".join(problems) if problems else "clean"
    print(f"  {name}: {peek(value)} - {status}")
    return not problems


def try_call(app_key, app_secret, token, token_param):
    params = {
        "method": "aliexpress.ds.product.get",
        "app_key": app_key,
        token_param: token,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "format": "json",
        "product_id": TEST_PRODUCT_ID,
        "ship_to_country": "GB",
        "target_currency": "GBP",
        "target_language": "en",
    }
    params["sign"] = sign_params(params, app_secret)
    resp = requests.post(GATEWAY, data=params, timeout=30)
    print(f"\n--- token sent as '{token_param}' -> HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        print(resp.text[:500])
        return False
    err = body.get("error_response")
    if err:
        print(f"    error code={err.get('code')} msg={err.get('msg')} sub={err.get('sub_code')} {err.get('sub_msg')}")
        return str(err.get("code") or "") or "error"
    text = json.dumps(body)
    title = ""
    node = body.get("aliexpress_ds_product_get_response") or {}
    result = (node.get("result") or {})
    base = result.get("ae_item_base_info_dto") or result.get("ae_item_base_info_d_t_o") or {}
    title = base.get("subject") or ""
    print(f"    SUCCESS - response {len(text)} chars, product title: {title[:80] or '(not found in expected field - paste this output to Claude)'}")
    if not title:
        print("    raw start:", text[:400])
    return True


def ask(label, current):
    if current:
        return current
    try:
        return input(f"Paste {label} and press Enter: ").strip()
    except EOFError:
        return ""


def main():
    # Env vars are used if present; otherwise the script just asks.
    app_key = ask("ALI_APP_KEY (App Key)", os.getenv("ALI_APP_KEY") or "")
    app_secret = ask("ALI_APP_SECRET (App Secret)", os.getenv("ALI_APP_SECRET") or "")
    token = ask("ALI_ACCESS_TOKEN (access token)", os.getenv("ALI_ACCESS_TOKEN") or "")
    if not (app_key and app_secret and token):
        print("All three values are needed - run again and paste each when asked.")
        sys.exit(1)

    print("Value check:")
    clean = all([hygiene("ALI_APP_KEY", app_key),
                 hygiene("ALI_APP_SECRET", app_secret),
                 hygiene("ALI_ACCESS_TOKEN", token)])
    if not clean:
        print("  -> trying again with whitespace stripped automatically")
    app_key, app_secret, token = app_key.strip(), app_secret.strip(), token.strip()

    ok_access = try_call(app_key, app_secret, token, "access_token")
    ok_session = try_call(app_key, app_secret, token, "session")

    print("\n=== VERDICT ===")
    codes = {r for r in (ok_access, ok_session) if isinstance(r, str)}
    if "InvalidAppKey" in codes:
        print("The APP KEY itself was rejected before the token was even looked at - re-copy "
              "ALI_APP_KEY (and ALI_APP_SECRET) from the app console on open.aliexpress.com.")
        return
    if "IncompleteSignature" in codes or "InvalidSignature" in codes:
        print("The signature was rejected - ALI_APP_SECRET is wrong. Re-copy it from the app console.")
        return
    ok_access = ok_access is True
    ok_session = ok_session is True
    if ok_access and ok_session:
        print("Token is VALID and the gateway accepts both parameter names. No client change needed.")
    elif ok_session and not ok_access:
        print("Token is VALID but only as 'session' - the pipeline client must be updated (tell Claude).")
    elif ok_access and not ok_session:
        print("Token is VALID as 'access_token' (what the pipeline already sends). If the GitHub run still "
              "fails, the GitHub secret value differs from what you used here - re-save ALI_ACCESS_TOKEN.")
    else:
        print("Token REJECTED both ways. Either this token was superseded (each browser authorization "
              "invalidates earlier tokens - if you did the authorize step more than once, only the LAST "
              "token works), or it belongs to a different app than this ALI_APP_KEY. Re-do the browser "
              "authorization once, exchange the code with get_aliexpress_token.py, and use that newest token.")


if __name__ == "__main__":
    main()
