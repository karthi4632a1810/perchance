"""
Perchance AI Agent client (ai-agent.perchance.org).
Reverse-engineered from perchance_ai network traces (minimal#edit).
"""

import json
import os
import secrets
import time
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")

DEFAULTS = {
    "PERCHANCE_AGENT_BASE_URL": "https://ai-agent.perchance.org",
    "PERCHANCE_CF_CLEARANCE": "",
    "PERCHANCE_USER_AGENT": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "PERCHANCE_GENERATOR": "minimal",
}


def load_config():
    config = dict(DEFAULTS)
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip().strip("\"'")
    except FileNotFoundError:
        pass
    for k in DEFAULTS:
        if os.environ.get(k):
            config[k] = os.environ[k]
    return config


class PerchanceAgentClient:
    def __init__(self, config=None):
        self.config = config or load_config()
        self.base_url = self.config.get("PERCHANCE_AGENT_BASE_URL", "https://ai-agent.perchance.org")
        self.session_id = None
        self.routing_key = None
        self.tab_id = "t" + secrets.token_hex(12)
        self.page_id = "t" + secrets.token_hex(12)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.config.get("PERCHANCE_USER_AGENT", DEFAULTS["PERCHANCE_USER_AGENT"]),
            "Referer": f"https://perchance.org/{self.config.get('PERCHANCE_GENERATOR', 'minimal')}",
            "Origin": "https://perchance.org",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Sec-Ch-Ua": '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        })
        cf_clearance = self.config.get("PERCHANCE_CF_CLEARANCE")
        if cf_clearance:
            self.session.cookies.set("cf_clearance", cf_clearance, domain=".perchance.org")

    def _api_url(self, path, key=None):
        k = key or self.routing_key
        url = f"{self.base_url}/api/aiAgent/{path}"
        if k:
            sep = "&" if "?" in path else "?"
            url += f"{sep}k={k}"
        return url

    def start_session(self, generator_name=None):
        """Starts a new agent session on Perchance."""
        name = generator_name or self.config.get("PERCHANCE_GENERATOR", "minimal")
        payload = {
            "generatorName": name,
            "modelText": "",
            "outputTemplate": "",
            "dependencies": [],
            "client": {"loggedIn": False, "ownsGenerator": False},
            "protocolVersion": 3
        }
        res = self.session.post(self._api_url("start"), json=payload, timeout=15)
        res.raise_for_status()
        data = res.json()
        if not data.get("ok"):
            raise RuntimeError(f"Failed to start session: {data}")
        self.session_id = data["sessionId"]
        self.routing_key = res.headers.get("x-agent-routing-key") or data.get("routingKey")
        return {
            "sessionId": self.session_id,
            "routingKey": self.routing_key,
            "agentsMd": data.get("agentsMd")
        }

    def get_history(self, session_id=None, routing_key=None):
        """Fetches conversation history for a session."""
        sid = session_id or self.session_id
        rk = routing_key or self.routing_key
        if not sid:
            raise ValueError("session_id is required")
        res = self.session.post(
            self._api_url("history", key=rk),
            json={"sessionId": sid},
            timeout=15
        )
        res.raise_for_status()
        data = res.json()
        if res.headers.get("x-agent-routing-key"):
            self.routing_key = res.headers.get("x-agent-routing-key")
        return data


if __name__ == "__main__":
    import sys
    client = PerchanceAgentClient()
    print("[*] Starting session...")
    info = client.start_session()
    print(f"[+] Started session {info['sessionId']} (routing key: {info['routingKey']})")
    print("[*] Fetching history...")
    hist = client.get_history()
    print(f"[+] History entries: {len(hist.get('entries', []))}")
