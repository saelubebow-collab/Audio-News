name: audio-news

on:
  schedule:
    - cron: "0 21 * * *"
  workflow_dispatch:

permissions:
  contents: write
  pages: write
  id-token: write

jobs:
  build:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install feedparser requests beautifulsoup4 google-genai edge-tts

      - run: python generate.py
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}

      - run: |
          git config user.name "github-actions"
          git config user.email "github-actions@users.noreply.github.com"
          git add -A
          git commit -m "update episodes" || echo "no changes"
          git pull --rebase
          git push

      - run: |
          mkdir -p "_site/$PAGE_KEY"
          cp -r docs/. "_site/$PAGE_KEY/"
        env:
          PAGE_KEY: ${{ secrets.PAGE_KEY }}

      - uses: actions/upload-pages-artifact@v3
        with:
          path: _site

      - uses: actions/deploy-pages@v4
