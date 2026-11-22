# Antigravity Beeper & Matrix MCP Integration

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Protocol](https://img.shields.io/badge/MCP-Model_Context_Protocol-blueviolet)
![Matrix](https://img.shields.io/badge/Matrix-E2EE%20Olm%2FMegolm-0DBD8B?logo=matrix&logoColor=white)
![Beeper](https://img.shields.io/badge/Beeper-Mobile%20Sync-4F46E5)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green.svg)

An asynchronous Model Context Protocol (MCP) integration connecting AI coding assistants to Beeper and Matrix with end-to-end encryption (E2EE).

> **Compatibility**: Fully compliant with the **Model Context Protocol (MCP)** standard (`beeper_notify` works with any MCP client such as Claude Desktop, Cursor, or Windsurf). Reactive async wakeup (`beeper_ask`) is built specifically for **Google Antigravity** via its internal `agentapi`.

---

## Architecture & How It Works

```text
Antigravity Agent ──(MCP / stdio)──▶ Local Python Daemon ──(Matrix E2EE)──▶ Beeper (Phone)
                                           ▲                                      │
                                           └────────── Async Reply ───────────────┘
                                        (Auto-shuts down after 120s idle)
```

1. **Decoupled Architecture**: The MCP server (`mcp_server.py`) acts as a lightweight stdio bridge. It talks to an autonomous background daemon (`core/hub.py`) over authenticated local HTTP (`127.0.0.1:24680`).
2. **Dedicated E2EE Device**: The daemon runs with its own dedicated Matrix device ID and local SQLite cryptographic store (`data/crypto_store/`), preventing Megolm session ratchet conflicts with Beeper Desktop.
3. **Reactive Wakeup**: When an agent calls `beeper_ask`, the question's event ID is mapped to the current conversation ID. When you swipe and reply in Beeper on your phone, the daemon decrypts the message and invokes Antigravity's internal API (`agentapi send-message`) to instantly resume the conversation.
4. **Zero-Waste Lifecycle**: The daemon monitors activity and shuts down automatically after 120 seconds of idle time. Zero CPU or RAM is wasted when you are not actively interacting.
5. **Offline Recovery Queue**: If you reply on your phone while Antigravity is closed or restarting, the daemon safely stores your response in `data/offline_replies.json` and flushes it into the chat the moment the editor becomes reachable.

---

## Highlights

- **Instant Notifications (`beeper_notify`)**: Send real-time updates and task status directly to your phone.
- **Interactive Questions (`beeper_ask`)**: Ask questions asynchronously. As soon as you reply on Beeper, your message wakes up Antigravity.
- **Resource & Token Efficient**: Wakes up cleanly via event injection without polling loops wasting LLM tokens; background daemon automatically shuts down after 120 seconds of inactivity (zero idle RAM).
- **Windows System Tray**: Native Windows tray icon with right-click menu ("Quit Hub") to inspect activity or cleanly terminate the background service.
- **Interactive Setup Wizard**: Guided CLI setup for device registration, room selection, and E2EE key initialization.

---

## Prerequisites

### 1. Matrix & Beeper Setup
To ensure clean cryptographic separation, we recommend:
- **Bot Account**: A free, dedicated Matrix account (e.g. `@your_bot:matrix.org`) created on matrix.org or your custom homeserver.
- **Personal Account**: Your personal Beeper or Matrix account (e.g. `@you:beeper.com`).
- **Private Room**: Create a direct encrypted chat between your personal account and your bot account. The interactive wizard will let you select this room automatically.

### 2. System Requirements
- **Python**: Version 3.10 to 3.12 recommended.
- **Windows Pre-requisites**: Modern `matrix-nio` (>=0.24.0) uses **vodozemac** (Matrix's official Rust-based Olm/Megolm engine), providing pre-compiled Python ABI3 binary wheels on PyPI. It installs cleanly out-of-the-box on 64-bit Windows without requiring Visual C++ Build Tools or CMake.

---

## Getting Started

### 1. Installation

Clone the repository and install the dependencies:

```bash
# Clone repository and enter folder
git clone https://github.com/Thomas-dvd/Antigravity-Beeper-MCP.git
cd Antigravity-Beeper-MCP

# Optional: create a virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Guided Device Onboarding

Run the interactive setup wizard to authenticate your dedicated Matrix device and pick your notification room:

```bash
python cli/setup_device.py
```

* Credentials (tokens, device IDs, homeserver) are saved securely in `.env`.
* User preferences (tray icon, idle timeout) are saved in `data/config.json`.

### 3. Register in Antigravity / MCP Client

Add the server to your `%USERPROFILE%\.gemini\config\mcp_config.json` (or your client's MCP configuration file):

```json
{
  "mcpServers": {
    "beeper": {
      "command": "python",
      "args": ["-B", "C:\\path\\to\\Antigravity-Beeper-MCP\\mcp_server.py"]
    }
  }
}
```
*(If you used a virtual environment, set `"command"` to `C:\\path\\to\\antigravity-beeper-mcp\\.venv\\Scripts\\python.exe`).*

---

## Available Tools

### `beeper_notify`
One-way notification tool for task milestones, alerts, and build reports. Does not wait for user input and does not hold the background daemon open.

**Parameters**:
- `message` *(string, required)*: The text or Markdown summary to send.
- `title` *(string, optional)*: Short bold header for the notification (e.g. `"Build Finished"`).

*Example Use Case*: An agent finishes generating code or running unit tests, notifies your phone that the test suite passed, and finishes its turn.

---

### `beeper_ask`
Asynchronous, non-blocking question tool. Dispatches a question to your phone and links it to the active conversation session.

**Parameters**:
- `question` *(string, required)*: The specific decision or clarification needed from the user.
- `title` *(string, optional)*: Short bold context (e.g. `"❓ Architecture Decision"`).
- `conversation_id` *(string, optional)*: Target conversation ID (automatically detected if omitted).

*Example Use Case*: The agent reaches a design crossroad (e.g. choosing between SQLite or PostgreSQL). It asks on Beeper, allows you to continue your day, and when you swipe and reply *"PostgreSQL"* from your phone, the conversation wakes up and executes your choice.

---

## Configuration

Preferences can be modified anytime in `data/config.json`:

```json
{
  "show_tray_icon": true,
  "idle_timeout": 120
}
```

- `show_tray_icon` *(bool)*: Toggle the Windows system tray icon on or off (default: `true`).
- `idle_timeout` *(int)*: Seconds of inactivity before the daemon cleanly shuts down (default: `120`).

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
