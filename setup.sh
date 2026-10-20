#!/bin/bash
# setup.sh — One-time setup script for stock-monitor on OCI instance
# Run: bash setup.sh

set -e

echo "============================================"
echo " Stock Monitor — OCI Instance Setup"
echo "============================================"

PROJECT_DIR="/home/ubuntu/repos/stock-monitor"
cd "$PROJECT_DIR"

# ─── 1. System dependencies ────────────────────────────────────────────────
echo "[1/6] Installing system dependencies..."
sudo apt-get update -q
sudo apt-get install -y -q \
    python3.11 python3.11-venv python3-pip \
    libta-lib-dev \
    libxml2-dev libxslt1-dev \
    libfreetype6-dev libpng-dev \
    pkg-config gcc

# ─── 2. Python virtual environment ────────────────────────────────────────
echo "[2/6] Creating Python virtual environment..."
python3.11 -m venv .venv
source .venv/bin/activate

# ─── 3. Install Python packages ───────────────────────────────────────────
echo "[3/6] Installing Python packages..."
pip install --upgrade pip -q

# Try to install ta-lib (C library required)
pip install ta-lib-python 2>/dev/null || {
    echo "  ta-lib-python failed, trying alternate..."
    pip install pandas-ta 2>/dev/null || true
}

pip install -r requirements.txt -q
echo "  Python packages installed."

# ─── 4. Create config from example ────────────────────────────────────────
echo "[4/6] Setting up configuration..."
if [ ! -f "config/secrets.yaml" ]; then
    cp config/secrets.yaml.example config/secrets.yaml
    echo "  Created config/secrets.yaml — PLEASE EDIT with your API keys!"
else
    echo "  config/secrets.yaml already exists."
fi

# Create charts output dir
mkdir -p charts/output logs data

# ─── 5. Install systemd service ────────────────────────────────────────────
echo "[5/6] Installing systemd service..."
sudo cp systemd/stock-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
echo "  Service file installed."
echo "  (NOT starting yet — configure secrets.yaml first)"

# ─── 6. Upstox auth reminder ──────────────────────────────────────────────
echo ""
echo "============================================"
echo " NEXT STEPS (manual — required once)"
echo "============================================"
echo ""
echo "1. Edit config/secrets.yaml with your API keys:"
echo "   nano $PROJECT_DIR/config/secrets.yaml"
echo ""
echo "2. Complete Upstox OAuth2 authorization (run once per day):"
echo "   cd $PROJECT_DIR && source .venv/bin/activate"
echo "   python -m src.fetcher.upstox_auth_setup"
echo ""
echo "3. Test the scanner manually:"
echo "   python -c \"from src.scanner import Scanner; s = Scanner(); s.refresh_instrument_master()\""
echo ""
echo "4. Start the systemd service:"
echo "   sudo systemctl start stock-monitor"
echo "   sudo systemctl enable stock-monitor   # auto-start on reboot"
echo ""
echo "5. Monitor logs:"
echo "   tail -f $PROJECT_DIR/logs/stock_monitor.log"
echo "   journalctl -u stock-monitor -f"
echo ""
echo "============================================"
echo " Setup complete! Configure secrets and start."
echo "============================================"
