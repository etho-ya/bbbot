# Bybit Trading Bot
# how to launch
python -m app.main
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000


systemctl daemon-reload
systemctl restart trading-bot
systemctl status trading-bot
systemctl stop trading-bot