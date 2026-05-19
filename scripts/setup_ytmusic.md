# YouTube Music auth setup

1. Open https://music.youtube.com in your browser (logged in).
2. Open DevTools -> Network tab.
3. Filter requests by `/browse`. Click any POST.
4. Right-click -> Copy -> Copy Request Headers (as raw text).
5. Run:
   mkdir -p secrets
   uv run python -c "import ytmusicapi; ytmusicapi.setup(filepath='secrets/ytmusic_browser.json', headers_raw='''<paste here>''')"
6. Set in `.env`:
   YTMUSIC_BROWSER_JSON_PATH=./secrets/ytmusic_browser.json
7. Test: uv run python -c "from ytmusicapi import YTMusic; print(YTMusic('secrets/ytmusic_browser.json').get_history()[:3])"

Cookie session lasts ~2 years unless you log out of YouTube anywhere.
