"""
ProtonVPN connection check.
Verifies VPN is active and IP is non-US before allowing trades.
"""

import subprocess
import requests

# Countries that are generally OK for Polymarket access
BLOCKED_COUNTRIES = {"US", "United States"}


def check_vpn_active() -> dict:
    """
    Check if VPN is connected by verifying external IP geolocation.
    Returns dict with: connected (bool), ip, country, city
    """
    try:
        # Check external IP via ipinfo.io (fast, no auth needed)
        resp = requests.get("https://ipinfo.io/json", timeout=10)
        resp.raise_for_status()
        data = resp.json()

        ip = data.get("ip", "unknown")
        country = data.get("country", "unknown")
        city = data.get("city", "unknown")
        org = data.get("org", "")

        is_vpn = "proton" in org.lower() or "privacywall" in org.lower()
        is_blocked = country in BLOCKED_COUNTRIES

        if is_blocked:
            return {
                "connected": False,
                "reason": f"IP geolocates to {country} — connect ProtonVPN to a non-US server first",
                "ip": ip,
                "country": country,
                "city": city,
            }

        return {
            "connected": True,
            "ip": ip,
            "country": country,
            "city": city,
            "vpn_detected": is_vpn,
        }

    except requests.RequestException as e:
        return {
            "connected": False,
            "reason": f"Could not check IP: {e}. Is your internet/VPN connected?",
        }


def check_protonvpn_cli() -> bool:
    """
    Check if ProtonVPN CLI is connected (optional — works if protonvpn-cli is installed).
    Falls back gracefully if CLI is not available.
    """
    try:
        result = subprocess.run(
            ["protonvpn-cli", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "connected" in result.stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False  # CLI not installed, rely on IP check


def ensure_vpn(required: bool = True) -> bool:
    """
    Main VPN gate. Returns True if safe to trade, False if blocked.
    Prints status info.
    """
    if not required:
        print("[vpn] VPN check disabled — proceeding without VPN verification")
        return True

    print("[vpn] Checking VPN connection...")
    status = check_vpn_active()

    if status["connected"]:
        print(
            f"[vpn] OK — IP: {status['ip']} | "
            f"Location: {status.get('city', '?')}, {status['country']}"
        )
        if status.get("vpn_detected"):
            print("[vpn] ProtonVPN detected")
        return True
    else:
        print(f"[vpn] BLOCKED — {status['reason']}")
        print("[vpn] Start ProtonVPN and connect to a non-US server, then retry.")
        return False


if __name__ == "__main__":
    ensure_vpn(required=True)
