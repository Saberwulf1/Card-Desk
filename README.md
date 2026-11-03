# Card Desk

Inventory, buying and selling for a trading-card stand: Pokémon (English and Japanese), Yu-Gi-Oh!, One Piece, Gundam and Riftbound.

- **App:** `site/index.html`, one page with no build step. It signs in with Google (Firebase Auth) and saves to Cloud Firestore, with an offline cache so it works at shows without signal.
- **Prices:** `sync/build_catalog.py` downloads the free nightly TCGplayer data from [TCGCSV](https://tcgcsv.com) once a day and writes `site/catalog/` (searchable card list + price lookup). It never runs in the browser.
- **Nightly job:** `.github/workflows/deploy.yml` runs the script at 02:43 UTC and publishes the site to GitHub Pages. It also runs on every push, and can be run by hand from the Actions tab. If any game fails to download, nothing is deployed and the previous day's prices stay live.
- **Data privacy:** this repository holds only the app and public card prices. Inventory, purchases, sales and expenses live in Firestore, readable only by the owner's Google account (enforced by Firestore security rules set in the Firebase console).

`site/vendor/firebase.js` is the Firebase web SDK 13.2.0 (app, auth, firestore) bundled with esbuild, so the app works offline without a CDN.
