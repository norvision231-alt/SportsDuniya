name: SportsDuniya Auto News Bot

on:
  schedule:
    - cron: '*/20 * * * *'
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: sports-duniya-hourly-loop
  cancel-in-progress: false

jobs:
  post-news:
    runs-on: ubuntu-latest
    timeout-minutes: 340
    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Dependencies
        run: pip install -r requirements.txt

      - name: Run Hourly Sports Bot Loop
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHANNEL_ID: ${{ secrets.TELEGRAM_CHANNEL_ID }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: python -u bot.py
