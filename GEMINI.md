# 📱 Butler Pro Project Instructions

> [!IMPORTANT]
> This project follows the [Root A2A Guidelines](../GEMINI.md). All agent communications must be optimized for token efficiency.

This project is a Discord-based personal assistant bot running on Termux (Android S9). It manages news tracking, SRT train reservations, and device status monitoring.

## 🏗 Project Architecture

- `butler_pro.py`: Main entry point, Discord client, event handlers, and background tasks (News loop, SRT loop, Subscription loop).
- `subscription_manager.py`: Orchestrates subscription-related tasks (notifications, keep-alive).
- `subscription_service.py`: Core logic for subscription data processing and notification delivery (Telegram/Discord).
- `api/flask_app.py`: Flask-based API server. Handles data persistence (YAML), remote message sending, and **provides data for the Web Dashboard**.
- `api/templates/`: Jinja2 templates for the Web Dashboard (`base.html`, `index.html`, `news.html`).
- `utils/tunnel_manager.py`: Automates Cloudflare tunnel setup and synchronizes the tunnel URL with the frontend via GitHub.
- `utils/system_status.py`: Provides raw system data (Battery, RAM, CPU) for both Discord and Web.
- `srt_service.py`: SRT reservation UI logic (Views and Modals) and reservation queue management.
- `config_manager.py`: Persistence layer for news keywords, SRT stations, reservation queue, and model settings.
- `news.json`, `reservations.json`, `keywords.json`, `stations.json`, `model_config.json`: Local storage files.
- `data/subscriptions.yaml`, `data/users.yaml`: Storage for the subscription management system.

## 🚀 Key Features & Command Locations

### 1. Web Dashboard (New!)
A modern, 3-pane responsive web interface for monitoring and control.
- **Architecture**: Flask (Backend) + Tailwind CSS (Styling) + Vanilla JS (Logic).
- **Layout**:
    - **Left Sidebar**: Search, Dark/Light mode toggle, Page navigation (Home, News).
    - **Main Content**: Dynamic page rendering with scrollable areas.
    - **Right Sidebar**: 
        - **Graph View**: Interactive node map (Vis.js) showing Keywords and Train tasks.
        - **Table of Contents (ToC)**: Automatic heading detection for quick navigation.
- **Real-time Integration**:
    - `/api/system_status`: Fetches real-time S9 battery/memory data every 30s.
    - `/api/graph_data`: Dynamically builds the node graph from `keywords.json` and `reservations.json`.

### 2. News Room (Web Optimized)
Categorized news feed with high scannability.
- **Categorization**: Grouped by keyword with sticky headers.
- **UX**: Each category is contained in a scrollable frame to keep the page length manageable.
- **Latest Badge**: Automatically marks news published within the last 1 hour as "LATEST".
- **Enhanced Cards**: Displays title, publisher, and publication time with direct external links.

### 3. Subscription Management (`subscription_manager.py`, `subscription_service.py`, `api/`)
- **Hybrid Architecture**: Streamlit Cloud (Frontend) + S9 Butler API (Backend/DB).
- **Data Persistence**: Uses local YAML files via Flask API, bypassing Streamlit's filesystem limitations.
- **Automated Notifications**: Sends alerts 30, 7, 1, and 0 days before expiration via Telegram and Discord.
- **Tunnel Automation**: `tunnel_manager.py` automatically updates the API URL in the frontend repo and pushes to GitHub.
- **Keep-Alive**: Periodically pings the Streamlit app to prevent sleep mode.

### 2. SRT Reservation System (`srt_service.py`, `butler_pro.py`)
- **Queue Management**: Supports up to 3 concurrent tasks per user.
- **Persistence**: Saved automatically to `reservations.json` via `config_manager.py`.
- **Stations**: Dynamically managed via Discord commands.
- **Commands (SRT Channel)**:
    - `!srt`: Opens the main reservation menu.
    - `!역 리스트`: Shows available stations.
    - `!역 추가 [Name]`: Adds a new station.
    - `!역 삭제 [Name]`: Removes a station.

### 2. News Tracking (`butler_pro.py`)
- **Keywords**: Dynamically managed, used by `news_loop` (30 min interval).
- **Commands (News Channel)**:
    - `!뉴스 리스트`: Lists tracked keywords.
    - `!뉴스 추가 [Keyword]`: Adds a keyword.
    - `!뉴스 삭제 [Keyword]`: Deletes a keyword.

### 3. AI Chat & Model Selection (`butler_pro.py`)
- **Engine**: Gemini (via `ask_gemini` function).
- **Models**: Dynamically selectable (`gemini-1.5-flash`, `gemini-1.5-flash-8b`, `gemini-2.0-flash`).
- **Commands (Chat Channel)**:
    - `!모델 리스트`: Shows available models and the current one.
    - `!모델 설정 [ModelID]`: Changes the active AI model.

### 4. Device Status (`butler_pro.py`)
- **Direct Handling**: Requests containing "battery", "temp", "status" skip AI to save tokens.
- **Format**: Returns a Discord Embed with percentage, temperature, health, and status.

### 5. A2A Manager-Coder System (`a2a_engine.py`, `butler_pro.py`)
- **Engine**: `A2AEngine` class manages the dual-agent workflow.
- **Manager Agent**: Analyzes requests and creates a technical design (JSON).
- **Coder Agent**: Implements Python code based on the design (JSON).
- **Validation**: Automatic syntax check via `py_compile` with a 3-retry self-correction loop.
- **Command (Chat Channel)**:
    - `!a2a [Request]`: Starts the automated design and coding process.

## 🛠 Development Guidelines

- **Blocking Calls**: NEVER make blocking network calls in the main event loop. Use `asyncio.to_thread` for SRT API calls or other synchronous I/O.
- **Persistence**: Always call `save_X()` from `config_manager.py` after modifying shared state (queue, keywords, etc.).
- **Global Variables**: Use `global` keyword when updating shared state like `MODEL_NAME` or `reservation_queue` within event handlers.
- **Error Handling**: Use `try-except` blocks around SRT API calls as they are prone to network timeouts or authentication failures.
