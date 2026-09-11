# Sorter Four

Standalone Windows build of the OPEX Sorter 4 barcode routing service.

## Getting the executable

Every push to `main` triggers a GitHub Actions build. To download the latest build:

1. Go to the **Actions** tab of this repo.
2. Open the most recent successful **Build Windows Executable** run.
3. Under **Artifacts**, download `SorterFour-windows`.
4. Unzip it — you'll get `SorterFour.exe`, ready to run on Windows (no Python install required).

## Manual trigger

You can also trigger a build without pushing new code: go to **Actions** → **Build Windows Executable** → **Run workflow**.
