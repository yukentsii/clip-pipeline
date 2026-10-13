"""One-time OAuth consent to obtain a YouTube refresh token.

1. Google Cloud Console -> enable "YouTube Data API v3" -> OAuth client ID (type: Desktop app)
2. Download the client JSON, then run (on a machine with a browser):
       python -m clip_pipeline.publish.scripts.youtube_auth path/to/client_secret.json
3. Put the printed values in .env (or into an account: PATCH /accounts/<id> with them as credentials)
"""
from __future__ import annotations

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

# upload to post clips; readonly so the service can read view counts and subscribers for Insights
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def main(path: str) -> None:
    flow = InstalledAppFlow.from_client_secrets_file(path, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    with open(path, encoding="utf-8") as fh:
        client = json.load(fh).get("installed", {})
    print(f"YOUTUBE_CLIENT_ID={client.get('client_id', creds.client_id)}")
    print(f"YOUTUBE_CLIENT_SECRET={client.get('client_secret', creds.client_secret)}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
