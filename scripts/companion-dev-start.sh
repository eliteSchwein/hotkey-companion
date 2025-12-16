rm -rf .HotkeyCompanion-env
python3 -m venv .HotkeyCompanion-env
source .HotkeyCompanion-env/bin/activate
pip install --upgrade pip setuptools
pip install -r ./scripts/companion-requirements.txt --prefer-binary