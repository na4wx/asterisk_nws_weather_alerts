# Main menu
pico2wave -l en-US -w /tmp/same_main_raw.wav \
"(...)NWS SAME code subscription menu... Press 1 to add coverage by zip code.. Press 2 to remove a code.. Press 3 to hear a list of your codes......."
sox /tmp/same_main_raw.wav -r 16000 -c 1 -b 16 -e signed-integer \
/var/lib/asterisk/sounds/custom/same-main-menu.wav norm -3
mv /var/lib/asterisk/sounds/custom/same-main-menu.wav \
   /var/lib/asterisk/sounds/custom/same-main-menu.wav16
rm -f /tmp/same_main_raw.wav

