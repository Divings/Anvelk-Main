#!/usr/bin/bash
set -e

cd /mnt/Dail/Dail-AI/
git pull
cd Linux
cp GeminiBot.py /mnt/Dail/Dail-Core/
cp pack/* /mnt/Dail/Dail-Core/pack/
cd /mnt/Dail/Dail-Core/
bash tools/rename_bot.sh
