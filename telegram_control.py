"""
Telegram Control Bot — Claude-powered remote control for Polymarket Bot.

Send messages from your phone, Claude reads logs/code, makes fixes, deploys.
Runs as a separate systemd service on the same EC2 server.

Commands:
  /status  — Bot status, P&L, recent trades
  /logs    — Last 20 log lines
  /restart — Restart the trading bot
  /pause   — Stop the trading bot
  /resume  — Start the trading bot
  /help    — Show available commands

Or just send natural language:
  "why aren't we getting any fills?"
  "increase the edge threshold to 12%"
  "show me the last 5 trades"
  "what's the weather forecast for Dallas?"

Claude has access to read files, edit code, check logs, and restart the bot.
"""

import os
import json
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

BOT_DIR = Path(__file__).parent.resolve()
POLL_INTERVAL = 2  # seconds between Telegram update checks
MAX_MESSAGE_LENGTH = 4000  # Telegram max is 4096

# ── Telegram API helpers ──

def tg_request(method: str, data: dict = None) -> dict:
    """Make a Telegram Bot API request."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    if data:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def send_message(chat_id: str, text: str):
    """Send a message, splitting if too long."""
    # Split long messages
    chunks = []
    while len(text) > MAX_MESSAGE_LENGTH:
        # Find a good split point
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)

    for chunk in chunks:
        if chunk.strip():
            tg_request("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })


def send_typing(chat_id: str):
    """Show typing indicator."""
    try:
        tg_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


# ── Tool implementations (what Claude can do) ──

def tool_read_logs(lines: int = 30, service: str = "polymarket-bot") -> str:
    """Read recent bot logs from journalctl."""
    try:
        result = subprocess.run(
            ["sudo", "journalctl", "-u", service, "--no-pager", "-n", str(lines)],
            capture_output=True, text=True, timeout=10
        )
        raw = result.stdout or result.stderr or "No logs found"
        # Clean up systemd prefix noise for readability
        cleaned = []
        for line in raw.strip().split("\n"):
            # Remove the "Mar 16 00:32:24 ip-172-31-39-190 python[4934]: " prefix
            if "python[" in line:
                parts = line.split("]: ", 1)
                cleaned.append(parts[1] if len(parts) > 1 else line)
            elif "systemd[" in line:
                if "Started" in line or "Stopped" in line:
                    cleaned.append(line.split("]: ", 1)[-1])
            else:
                cleaned.append(line)
        return "\n".join(cleaned)
    except Exception as e:
        return f"Error reading logs: {e}"


def tool_bot_status() -> str:
    """Get bot service status in human-readable format."""
    lines = []

    # Check both services
    for service, label in [("polymarket-bot", "Weather Bot"), ("crypto-maker", "Crypto Maker")]:
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "is-active", service],
                capture_output=True, text=True, timeout=5
            )
            is_active = result.stdout.strip() == "active"

            if is_active:
                # Get uptime
                uptime_result = subprocess.run(
                    ["sudo", "systemctl", "show", service, "--property=ActiveEnterTimestamp"],
                    capture_output=True, text=True, timeout=5
                )
                timestamp = uptime_result.stdout.strip().split("=", 1)[-1] if uptime_result.stdout else "unknown"

                # Get memory
                mem_result = subprocess.run(
                    ["sudo", "systemctl", "show", service, "--property=MemoryCurrent"],
                    capture_output=True, text=True, timeout=5
                )
                mem_bytes = mem_result.stdout.strip().split("=", 1)[-1] if mem_result.stdout else "0"
                try:
                    mem_mb = int(mem_bytes) / (1024 * 1024)
                    mem_str = f"{mem_mb:.0f}MB"
                except (ValueError, TypeError):
                    mem_str = "?"

                lines.append(f"🟢 {label}: Running ({mem_str} RAM)")
                lines.append(f"   Started: {timestamp}")
            else:
                lines.append(f"🔴 {label}: Stopped")
        except Exception:
            lines.append(f"⚪ {label}: Unknown")

    # Separate scoreboards for each bot service
    for service, header in [("polymarket-bot", "🌤️ WEATHER SCOREBOARD"), ("crypto-maker", "📈 CRYPTO SCOREBOARD")]:
        try:
            log_result = subprocess.run(
                ["sudo", "journalctl", "-u", service, "--no-pager", "-n", "20"],
                capture_output=True, text=True, timeout=5
            )
            log_lines = log_result.stdout.strip().split("\n")
            bankroll_info = None
            for log_line in reversed(log_lines):
                if "Bankroll:" in log_line and "P&L:" in log_line:
                    bankroll_info = log_line.split("Bankroll:", 1)[-1].strip()
                    break

            lines.append(f"\n{header}")
            if bankroll_info:
                for part in bankroll_info.split("|"):
                    part = part.strip()
                    if part.startswith("$") or part.startswith("-$"):
                        lines.append(f"  💰 Bankroll: {part}")
                    elif "P&L" in part:
                        lines.append(f"  📊 {part}")
                    elif "W/L" in part:
                        lines.append(f"  🎯 {part}")
                    elif "Pending" in part:
                        lines.append(f"  ⏳ {part}")
                    else:
                        lines.append(f"  💰 Bankroll: {part}")
            else:
                lines.append("  No data yet")
        except Exception:
            lines.append(f"\n{header}")
            lines.append("  Could not read logs")

    # Bond Grinder scoreboard (from data file)
    lines.append("\n🏦 BOND GRINDER SCOREBOARD")
    try:
        import json as _json
        pnl_file = BOT_DIR / "data" / "bond_pnl.json"
        if pnl_file.exists():
            bp = _json.loads(pnl_file.read_text())
            net = bp.get("total_returned", 0) - bp.get("total_invested", 0)
            wr = bp["wins"] / (bp["wins"] + bp["losses"]) * 100 if (bp["wins"] + bp["losses"]) > 0 else 0
            lines.append(f"  📊 P&L: ${net:+.2f}")
            lines.append(f"  🎯 W/L: {bp['wins']}/{bp['losses']} ({wr:.0f}%)")
            lines.append(f"  ⏳ Open: {bp.get('positions_open', 0)}")
        else:
            lines.append("  No data yet")
    except Exception:
        lines.append("  Could not read data")

    # AI Mispricing scoreboard (from data file)
    lines.append("\n🔍 AI MISPRICING SCOREBOARD")
    try:
        import json as _json
        trades_file = BOT_DIR / "data" / "ai_mispricing_trades.json"
        if trades_file.exists():
            trades = _json.loads(trades_file.read_text())
            total_invested = sum(t.get("bet_size", 0) for t in trades if t.get("status") in ("placed", "paper_placed"))
            wins = sum(1 for t in trades if t.get("resolved") and t.get("won"))
            losses = sum(1 for t in trades if t.get("resolved") and not t.get("won"))
            open_pos = sum(1 for t in trades if t.get("status") in ("placed", "paper_placed") and not t.get("resolved"))
            total_returned = sum(t.get("payout", 0) for t in trades if t.get("resolved") and t.get("won"))
            net = total_returned - total_invested
            lines.append(f"  📊 P&L: ${net:+.2f}")
            lines.append(f"  🎯 W/L: {wins}/{losses}")
            lines.append(f"  ⏳ Open: {open_pos}")
            lines.append(f"  💰 Deployed: ${total_invested:.2f}")
        else:
            lines.append("  No data yet")
    except Exception:
        lines.append("  Could not read data")

    # V7 Dump-and-Hedge scoreboard (from logs)
    lines.append("\n⚡ V7 DUMP-AND-HEDGE")
    try:
        v7_result = subprocess.run(
            ["sudo", "journalctl", "-u", "polymarket-v7", "--no-pager", "-n", "20"],
            capture_output=True, text=True, timeout=5
        )
        v7_active = subprocess.run(
            ["sudo", "systemctl", "is-active", "polymarket-v7"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip() == "active"

        if v7_active:
            lines.append("  🟢 Running")
            v7_lines = v7_result.stdout.strip().split("\n")
            for vl in reversed(v7_lines):
                if "P&L" in vl or "pnl" in vl.lower():
                    info = vl.split("]: ", 1)[-1] if "]: " in vl else vl
                    lines.append(f"  📊 {info.strip()[:80]}")
                    break
        else:
            lines.append("  🔴 Stopped")
    except Exception:
        lines.append("  Status unknown")

    return "\n".join(lines) if lines else "Could not get status"


def tool_restart_bot() -> str:
    """Restart the trading bot service."""
    try:
        subprocess.run(["sudo", "systemctl", "restart", "polymarket-bot"], timeout=10)
        time.sleep(2)
        return "✅ Weather bot restarted!\n\n" + tool_bot_status()
    except Exception as e:
        return f"Error restarting: {e}"


def tool_pause_bot() -> str:
    """Stop the trading bot service."""
    try:
        subprocess.run(["sudo", "systemctl", "stop", "polymarket-bot"], timeout=10)
        return "⏸️ Weather bot paused. Send /resume to start again."
    except Exception as e:
        return f"Error stopping: {e}"


def tool_resume_bot() -> str:
    """Start the trading bot service."""
    try:
        subprocess.run(["sudo", "systemctl", "start", "polymarket-bot"], timeout=10)
        time.sleep(2)
        return tool_bot_status()
    except Exception as e:
        return f"Error starting: {e}"


def tool_read_file(file_path: str) -> str:
    """Read a file from the bot directory."""
    try:
        # Security: only allow reading files within the bot directory
        full_path = (BOT_DIR / file_path).resolve()
        if not str(full_path).startswith(str(BOT_DIR)):
            return "Error: Can only read files within the bot directory"
        if not full_path.exists():
            return f"File not found: {file_path}"
        content = full_path.read_text()
        if len(content) > 8000:
            return content[:8000] + "\n... (truncated)"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


def tool_edit_file(file_path: str, old_text: str, new_text: str) -> str:
    """Edit a file by replacing old_text with new_text."""
    try:
        full_path = (BOT_DIR / file_path).resolve()
        if not str(full_path).startswith(str(BOT_DIR)):
            return "Error: Can only edit files within the bot directory"
        if not full_path.exists():
            return f"File not found: {file_path}"

        content = full_path.read_text()
        if old_text not in content:
            return f"Error: old_text not found in {file_path}"

        count = content.count(old_text)
        new_content = content.replace(old_text, new_text, 1)
        full_path.write_text(new_content)
        return f"Edited {file_path} ({count} occurrence(s) found, replaced first)"
    except Exception as e:
        return f"Error editing file: {e}"


def tool_run_command(command: str) -> str:
    """Run a shell command (limited to safe operations)."""
    # Block dangerous commands
    dangerous = ["rm -rf", "mkfs", "dd if=", "> /dev/", "shutdown", "reboot",
                 "passwd", "chmod 777", "curl | bash", "wget | bash"]
    for d in dangerous:
        if d in command:
            return f"Blocked dangerous command: {command}"

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd=str(BOT_DIR)
        )
        output = result.stdout + result.stderr
        if len(output) > 4000:
            output = output[:4000] + "\n... (truncated)"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out (30s limit)"
    except Exception as e:
        return f"Error: {e}"


def tool_list_files(directory: str = ".") -> str:
    """List files in a directory."""
    try:
        full_path = (BOT_DIR / directory).resolve()
        if not str(full_path).startswith(str(BOT_DIR)):
            return "Error: Can only list files within the bot directory"
        result = subprocess.run(
            ["ls", "-la", str(full_path)],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout or result.stderr
    except Exception as e:
        return f"Error: {e}"


def tool_deploy_restart() -> str:
    """After editing files, restart the bot to apply changes."""
    return tool_restart_bot()


# ── Claude API with tool use ──

TOOLS = [
    {
        "name": "read_logs",
        "description": "Read recent trading bot logs from systemd journal. Shows scan results, trades, errors.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lines": {"type": "integer", "description": "Number of log lines to read (default 30)", "default": 30}
            }
        }
    },
    {
        "name": "bot_status",
        "description": "Get the trading bot's systemd service status (running/stopped, uptime, memory usage).",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "restart_bot",
        "description": "Restart the trading bot service. Use after making code changes.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "pause_bot",
        "description": "Stop the trading bot. It will not trade until resumed.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "resume_bot",
        "description": "Start/resume the trading bot.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "read_file",
        "description": "Read a source code file from the bot directory. Use to understand how the bot works or diagnose issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Relative path from bot directory, e.g. 'arb_engine_v4.py'"}
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "edit_file",
        "description": "Edit a source code file by replacing old_text with new_text. Use to fix bugs, change parameters, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Relative path to file"},
                "old_text": {"type": "string", "description": "Exact text to find and replace"},
                "new_text": {"type": "string", "description": "New text to replace with"}
            },
            "required": ["file_path", "old_text", "new_text"]
        }
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the bot directory. Use for checking Python versions, pip packages, disk space, network, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in a directory within the bot project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Relative directory path (default '.')", "default": "."}
            }
        }
    },
    {
        "name": "deploy_restart",
        "description": "Restart the bot after making code changes to apply them.",
        "input_schema": {"type": "object", "properties": {}}
    },
]

TOOL_HANDLERS = {
    "read_logs": lambda args: tool_read_logs(args.get("lines", 30)),
    "bot_status": lambda args: tool_bot_status(),
    "restart_bot": lambda args: tool_restart_bot(),
    "pause_bot": lambda args: tool_pause_bot(),
    "resume_bot": lambda args: tool_resume_bot(),
    "read_file": lambda args: tool_read_file(args["file_path"]),
    "edit_file": lambda args: tool_edit_file(args["file_path"], args["old_text"], args["new_text"]),
    "run_command": lambda args: tool_run_command(args["command"]),
    "list_files": lambda args: tool_list_files(args.get("directory", ".")),
    "deploy_restart": lambda args: tool_deploy_restart(),
}

SYSTEM_PROMPT = """You are the remote control AI for a Polymarket trading bot running on an AWS EC2 server.

The bot trades weather temperature brackets on Polymarket using a GFS 31-member ensemble forecast model.
It's written in Python and runs as a systemd service called 'polymarket-bot'.

Key files:
- arb_engine_v4.py — Main trading engine (weather scoring, order execution, bankroll management)
- bracket_model.py — Probability models (ensemble counting, normal distribution fallback)
- bracket_markets.py — Market discovery from Polymarket Gamma API
- noaa_feed.py — Weather data (GFS ensemble from Open-Meteo + NOAA forecasts)
- bot.py — CLI entry point
- telegram_alerts.py — Trade notification alerts
- .env — Configuration (DO NOT share secrets)

You can:
1. Read logs to diagnose issues
2. Check bot status
3. Read and edit source code files
4. Restart the bot after changes
5. Run shell commands for debugging

Keep responses concise — this is Telegram, not a full IDE. Use short paragraphs.
When making code changes, always explain what you're changing and why, then restart the bot.
Never share API keys, private keys, or other secrets."""


def call_claude(user_message: str, chat_id: str) -> str:
    """Call Claude API with tool use, handle tool calls in a loop."""
    messages = [{"role": "user", "content": user_message}]

    for iteration in range(8):  # Max 8 tool-use rounds
        # Call Claude API
        request_body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": messages,
        }

        body = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                response = json.loads(resp.read().decode())
        except Exception as e:
            return f"Claude API error: {e}"

        # Process response
        stop_reason = response.get("stop_reason", "end_turn")
        content_blocks = response.get("content", [])

        if stop_reason == "tool_use":
            # Extract tool calls and execute them
            tool_results = []
            for block in content_blocks:
                if block.get("type") == "tool_use":
                    tool_name = block["name"]
                    tool_input = block.get("input", {})
                    tool_id = block["id"]

                    # Show typing while executing tool
                    send_typing(chat_id)

                    # Execute the tool
                    handler = TOOL_HANDLERS.get(tool_name)
                    if handler:
                        try:
                            result = handler(tool_input)
                        except Exception as e:
                            result = f"Tool error: {e}"
                    else:
                        result = f"Unknown tool: {tool_name}"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": str(result),
                    })

            # Add assistant message and tool results to conversation
            messages.append({"role": "assistant", "content": content_blocks})
            messages.append({"role": "user", "content": tool_results})

        else:
            # Final text response
            text_parts = []
            for block in content_blocks:
                if block.get("type") == "text":
                    text_parts.append(block["text"])
            return "\n".join(text_parts) if text_parts else "(no response)"

    return "Reached max tool iterations. Try a simpler request."


# ── Quick command handlers (no Claude needed) ──

def handle_quick_command(command: str) -> str | None:
    """Handle simple commands without calling Claude API."""
    cmd = command.strip().lower()

    if cmd == "/status":
        return tool_bot_status()
    elif cmd == "/logs":
        return tool_read_logs(20)
    elif cmd == "/restart":
        return tool_restart_bot()
    elif cmd == "/pause":
        return tool_pause_bot()
    elif cmd == "/resume":
        return tool_resume_bot()
    elif cmd == "/help":
        return (
            "*Polymarket Bot Remote Control*\n\n"
            "*Quick commands:*\n"
            "`/status` — Bot status & uptime\n"
            "`/logs` — Recent log lines\n"
            "`/restart` — Restart the bot\n"
            "`/pause` — Stop trading\n"
            "`/resume` — Resume trading\n"
            "`/pnl` — All strategy P&L\n"
            "`approve 1` — Approve AI mispricing trade #1\n"
            "`approve all` — Approve all pending AI trades\n"
            "`/help` — This message\n\n"
            "*Or ask me anything:*\n"
            "\"why aren't we getting fills?\"\n"
            "\"show today's P&L\"\n\n"
            "I can read code, edit files, check logs, and restart the bot."
        )
    elif cmd == "/pnl":
        return _get_master_pnl()

    # Handle "approve N" or "approve all" for AI mispricing
    if cmd.startswith("approve "):
        return _handle_approve(command.strip())

    return None  # Not a quick command — use Claude


def _get_master_pnl() -> str:
    """Master P&L across all strategies."""
    import json as _json
    lines = ["📊 *MASTER P&L*\n"]

    # Bond Grinder
    try:
        pnl_file = BOT_DIR / "data" / "bond_pnl.json"
        if pnl_file.exists():
            bp = _json.loads(pnl_file.read_text())
            net = bp.get("total_returned", 0) - bp.get("total_invested", 0)
            lines.append(f"🏦 Bonds: ${net:+.2f} ({bp['wins']}W/{bp['losses']}L)")
        else:
            lines.append("🏦 Bonds: No data")
    except Exception:
        lines.append("🏦 Bonds: Error")

    # AI Mispricing
    try:
        trades_file = BOT_DIR / "data" / "ai_mispricing_trades.json"
        if trades_file.exists():
            trades = _json.loads(trades_file.read_text())
            invested = sum(t.get("bet_size", 0) for t in trades if t.get("status") in ("placed", "paper_placed"))
            returned = sum(t.get("payout", 0) for t in trades if t.get("resolved") and t.get("won"))
            wins = sum(1 for t in trades if t.get("resolved") and t.get("won"))
            losses = sum(1 for t in trades if t.get("resolved") and not t.get("won"))
            open_p = sum(1 for t in trades if not t.get("resolved") and t.get("status") in ("placed", "paper_placed"))
            lines.append(f"🔍 AI Mispricing: ${returned - invested:+.2f} ({wins}W/{losses}L, {open_p} open)")
        else:
            lines.append("🔍 AI Mispricing: No data")
    except Exception:
        lines.append("🔍 AI Mispricing: Error")

    # V7
    try:
        v7_active = subprocess.run(
            ["sudo", "systemctl", "is-active", "polymarket-v7"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip() == "active"
        lines.append(f"⚡ V7: {'Running' if v7_active else 'Stopped'}")
    except Exception:
        lines.append("⚡ V7: Unknown")

    return "\n".join(lines)


def _handle_approve(text: str) -> str:
    """Handle approve commands for AI mispricing trades."""
    import json as _json
    import sys

    parts = text.split()
    if len(parts) < 2:
        return "Usage: `approve 1` or `approve all`"

    try:
        # Import and run the approve function
        sys.path.insert(0, str(BOT_DIR))
        from ai_mispricing_scanner import approve_and_place, load_pending

        if parts[1].lower() == "all":
            pending = load_pending()
            unapproved = [p for p in pending if not p.get("approved")]
            if not unapproved:
                return "No pending trades to approve."
            approve_and_place(approve_all=True)
            return f"✅ Approved {len(unapproved)} AI mispricing trades!"
        else:
            indices = [int(x) for x in parts[1:]]
            approve_and_place(indices=indices)
            return f"✅ Approved trades: {indices}"
    except Exception as e:
        return f"Error approving: {e}"


# ── Main polling loop ──

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return
    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID not set in .env")
        return
    if not ANTHROPIC_API_KEY:
        print("WARNING: ANTHROPIC_API_KEY not set — AI features disabled, only quick commands work")

    print(f"Telegram Control Bot started")
    print(f"  Chat ID: {TELEGRAM_CHAT_ID}")
    print(f"  Bot dir: {BOT_DIR}")
    print(f"  Claude:  {'enabled' if ANTHROPIC_API_KEY else 'DISABLED (no API key)'}")

    # Send startup message
    send_message(TELEGRAM_CHAT_ID,
        "🤖 *Control Bot Online*\n"
        f"Claude AI: {'enabled' if ANTHROPIC_API_KEY else 'disabled'}\n"
        "Send /help for commands"
    )

    last_update_id = 0

    while True:
        try:
            # Long-poll for new messages
            params = {"offset": last_update_id + 1, "timeout": 30, "allowed_updates": ["message"]}
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url)

            with urllib.request.urlopen(req, timeout=35) as resp:
                updates = json.loads(resp.read().decode())

            if not updates.get("ok"):
                continue

            for update in updates.get("result", []):
                update_id = update["update_id"]
                last_update_id = max(last_update_id, update_id)

                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "").strip()

                # Security: only respond to the configured chat ID
                if chat_id != TELEGRAM_CHAT_ID or not text:
                    continue

                print(f"[telegram] Received: {text[:50]}")

                # Try quick command first
                quick_response = handle_quick_command(text)
                if quick_response is not None:
                    send_message(chat_id, quick_response)
                    continue

                # Use Claude for everything else
                if not ANTHROPIC_API_KEY:
                    send_message(chat_id, "AI features disabled — set ANTHROPIC_API_KEY in .env")
                    continue

                send_typing(chat_id)
                response = call_claude(text, chat_id)
                send_message(chat_id, response)

        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            print(f"[telegram] Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
