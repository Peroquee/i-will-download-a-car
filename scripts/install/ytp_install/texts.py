"""Every sentence the installer shows, in German and English.

Plain language on purpose: the reader may never have used Linux. Each question has a short
prompt (`<id>.ask`) and, where useful, a longer explanation of WHY the value is needed
(`<id>.why`) shown before it. tests/unit/test_installer_texts.py checks that both languages have
exactly the same keys and the same `{placeholders}`, and that nothing is empty.
"""

# ruff: noqa: E501

from __future__ import annotations

import string

TEXTS: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------------ general
    "title": {
        "de": "youtube-premium: Installation",
        "en": "youtube-premium: installation",
    },
    "intro": {
        "de": (
            "Dieser Assistent richtet alles ein: ein VPN, den Torrent-Client, die Webseite zum\n"
            "Hinzufügen von Medien und die Verbindung zu Jellyfin. Er stellt dir ein paar Fragen,\n"
            "danach läuft alles von selbst. Das dauert etwa 15 bis 30 Minuten.\n\n"
            "• Du kannst jederzeit mit Strg+C abbrechen und den Assistenten später noch einmal starten:\n"
            "  er macht dort weiter, wo er aufgehört hat.\n"
            "• In eckigen Klammern steht der vorgeschlagene Wert. Einfach Enter drücken, um ihn zu nehmen.\n"
            "• Passwörter siehst du beim Tippen nicht. Das ist normal."
        ),
        "en": (
            "This assistant sets everything up: a VPN, the torrent client, the web page for adding\n"
            "media and the connection to Jellyfin. It asks you a few questions, then everything runs\n"
            "by itself. It takes about 15 to 30 minutes.\n\n"
            "• You can stop at any time with Ctrl+C and start the assistant again later:\n"
            "  it carries on where it stopped.\n"
            "• The suggested value is shown in square brackets. Just press Enter to take it.\n"
            "• You do not see passwords while typing. That is normal."
        ),
    },
    "lang.ask": {
        "de": "Sprache / Language: [d]eutsch oder [e]nglish",
        "en": "Sprache / Language: [d]eutsch or [e]nglish",
    },
    "not_debian": {
        "de": 'Dieses Programm läuft nur auf Debian 12 ("bookworm"). Hier wurde {found} erkannt. Bitte eine neue Debian-12-VM anlegen (siehe docs/host-setup.md).',
        "en": 'This program only runs on Debian 12 ("bookworm"). Found here: {found}. Please create a fresh Debian 12 VM (see docs/host-setup.md).',
    },
    "dry_run": {
        "de": "TESTLAUF (--dry-run): Es wird nichts verändert. Befehle erscheinen mit einem + davor.",
        "en": "TEST RUN (--dry-run): nothing is changed. Commands are shown with a + in front.",
    },
    "yes_no": {"de": "j/n", "en": "y/n"},
    "yes_chars": {"de": "jJyY", "en": "yY"},
    "keep_secret": {
        "de": "Enter = den gespeicherten Wert behalten",
        "en": "Enter = keep the saved value",
    },
    "again": {"de": "Bitte noch einmal:", "en": "Please try again:"},
    "interrupted": {
        "de": "Abgebrochen. Starte ./install.sh später noch einmal; es macht dort weiter, wo es aufgehört hat.",
        "en": "Stopped. Run ./install.sh again later; it carries on where it stopped.",
    },
    # ------------------------------------------------------------------ errors in answers
    "err.empty": {"de": "Hier muss etwas stehen.", "en": "This cannot be empty."},
    "err.user": {
        "de": "Erlaubt sind 3 bis 64 Zeichen: Buchstaben, Ziffern, Punkt, Unterstrich und Bindestrich.",
        "en": "Allowed: 3 to 64 characters: letters, digits, dot, underscore and hyphen.",
    },
    "err.short": {
        "de": "Das ist zu kurz.",
        "en": "That is too short.",
    },
    "err.quote": {
        "de": "Das Zeichen ' (einfaches Anführungszeichen) kann hier nicht verwendet werden, weil es in der Einstellungsdatei Probleme macht. Bitte ein anderes Passwort wählen.",
        "en": "The character ' (single quote) cannot be used here because it breaks the settings file. Please choose another password.",
    },
    "err.control": {
        "de": "Keine Leerzeichen am Anfang oder Ende und keine Sonderzeichen wie Zeilenumbrüche.",
        "en": "No spaces at the start or end and no special characters such as line breaks.",
    },
    "err.differ": {
        "de": "Die beiden Eingaben sind verschieden.",
        "en": "The two entries are different.",
    },
    "err.folder": {
        "de": "Ein Ordnername darf kein / oder \\ enthalten, nicht mit - beginnen und nicht . oder .. sein.",
        "en": "A folder name must not contain / or \\, must not start with - and must not be . or ..",
    },
    "err.folder_twice": {
        "de": "Jede Mediensorte braucht ihren eigenen Ordner: zwei Namen sind gleich.",
        "en": "Every kind of media needs its own folder: two names are the same.",
    },
    "err.folder_downloads": {
        "de": '"downloads" ist für unfertige Downloads reserviert.',
        "en": '"downloads" is reserved for unfinished downloads.',
    },
    "err.path": {
        "de": "Bitte einen vollständigen Pfad angeben, der mit / beginnt (z. B. /data).",
        "en": "Please give a full path starting with / (for example /data).",
    },
    "err.ip": {
        "de": "Das ist keine gültige IPv4-Adresse (Beispiel: 192.168.178.20).",
        "en": "That is not a valid IPv4 address (example: 192.168.178.20).",
    },
    "err.subnet": {
        "de": "Das ist kein gültiges Netz (Beispiel: 192.168.178.0/24).",
        "en": "That is not a valid network (example: 192.168.178.0/24).",
    },
    "err.not_in_subnet": {
        "de": "Die Adresse liegt nicht in diesem Netz.",
        "en": "The address is not in that network.",
    },
    "err.url": {
        "de": "Bitte eine Adresse wie http://192.168.178.108:8096 angeben.",
        "en": "Please give an address like http://192.168.178.108:8096.",
    },
    "err.jellyfin_key": {
        "de": "Ein Jellyfin-Schlüssel besteht aus 32 Zeichen (Ziffern 0-9 und Buchstaben a-f).",
        "en": "A Jellyfin key has 32 characters (digits 0-9 and letters a-f).",
    },
    "err.prefix": {
        "de": "Bitte Adressen oder Präfixe angeben, getrennt durch Komma (Beispiel: 2003:abcd:1200::/56).",
        "en": "Please give addresses or prefixes separated by commas (example: 2003:abcd:1200::/56).",
    },
    "err.provider": {
        "de": "Der Name des Anbieters besteht aus Kleinbuchstaben, Ziffern, Leerzeichen, Punkt oder Bindestrich (z. B. mullvad).",
        "en": "The provider name is made of lower-case letters, digits, spaces, dots or hyphens (for example mullvad).",
    },
    "err.countries": {
        "de": "Bitte nur Ländernamen, getrennt durch Komma (z. B. Germany).",
        "en": "Please give country names only, separated by commas (for example Germany).",
    },
    "err.wg_addresses": {
        "de": "Bitte eine Adresse mit Netzmaske angeben (Beispiel: 10.64.222.21/32).",
        "en": "Please give an address with a prefix length (example: 10.64.222.21/32).",
    },
    "err.timezone": {
        "de": "Diese Zeitzone ist unbekannt (Beispiel: Europe/Berlin).",
        "en": "This time zone is unknown (example: Europe/Berlin).",
    },
    "err.ovpn_missing": {
        "de": "Diese Datei gibt es nicht oder sie ist nicht lesbar.",
        "en": "This file does not exist or cannot be read.",
    },
    "err.ovpn_content": {
        "de": 'Das sieht nicht nach einer .ovpn-Datei aus (es fehlt die Zeile "remote ...").',
        "en": 'This does not look like an .ovpn file (the line "remote ..." is missing).',
    },
    "err.choice": {
        "de": "Bitte {options} eingeben.",
        "en": "Please enter {options}.",
    },
    # ------------------------------------------------------------------ questions
    "vpn_kind.why": {
        "de": (
            "Das VPN ist der wichtigste Teil: Torrents laufen NUR durch den VPN-Tunnel. Fällt das VPN aus,\n"
            "hat der Torrent-Client gar kein Netz mehr. Dafür brauche ich die Zugangsdaten deines VPN-Anbieters."
        ),
        "en": (
            "The VPN is the most important part: torrents only run THROUGH the VPN tunnel. If the VPN\n"
            "drops, the torrent client has no network at all. For that I need your VPN provider's details."
        ),
    },
    "vpn_kind.ask": {
        "de": "Wie bekommst du die Zugangsdaten?\n  1 = Mein Anbieter gibt mir eine .ovpn-Datei (z. B. ExpressVPN)\n  2 = Mein Anbieter steht in der Liste von gluetun (z. B. Mullvad, Proton, NordVPN)\nAuswahl",
        "en": "How do you get your details?\n  1 = My provider gives me an .ovpn file (for example ExpressVPN)\n  2 = My provider is in gluetun's list (for example Mullvad, Proton, NordVPN)\nChoice",
    },
    "ovpn_path.why": {
        "de": (
            'ExpressVPN: Auf der Webseite deines Kontos unter "Auf weiteren Geräten einrichten" →\n'
            '"Manuelle Konfiguration" → "OpenVPN" gibt es eine .ovpn-Datei zum Herunterladen\n'
            "(ein Standort deiner Wahl). Kopiere sie auf diesen Computer (z. B. mit WinSCP oder\n"
            "`scp`) und gib hier den Pfad an. Die Datei enthält Schlüssel und bleibt geheim."
        ),
        "en": (
            'ExpressVPN: on your account web page under "Set up on more devices" →\n'
            '"Manual configuration" → "OpenVPN" there is an .ovpn file to download\n'
            "(one location of your choice). Copy it to this computer (for example with WinSCP or\n"
            "`scp`) and give the path here. The file contains keys and stays secret."
        ),
    },
    "ovpn_path.ask": {
        "de": "Pfad der .ovpn-Datei (z. B. /home/{user}/my_expressvpn_germany_udp.ovpn)",
        "en": "Path of the .ovpn file (for example /home/{user}/my_expressvpn_germany_udp.ovpn)",
    },
    "vpn_user.why": {
        "de": (
            "Das sind NICHT deine E-Mail-Adresse und dein Konto-Passwort. Bei ExpressVPN stehen die\n"
            "Zugangsdaten für die manuelle Konfiguration auf derselben Seite wie die .ovpn-Datei."
        ),
        "en": (
            "These are NOT your e-mail address and account password. With ExpressVPN the details for\n"
            "the manual configuration are on the same page as the .ovpn file."
        ),
    },
    "vpn_user.ask": {"de": "VPN-Benutzername", "en": "VPN user name"},
    "vpn_password.ask": {"de": "VPN-Passwort (unsichtbar)", "en": "VPN password (invisible)"},
    "vpn_provider.why": {
        "de": "Der Name muss genau so geschrieben sein wie in der Liste von gluetun: https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers",
        "en": "The name must be spelled exactly as in gluetun's list: https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers",
    },
    "vpn_provider.ask": {
        "de": "Name des Anbieters in Kleinbuchstaben (z. B. mullvad, protonvpn, nordvpn)",
        "en": "Provider name in lower case (for example mullvad, protonvpn, nordvpn)",
    },
    "vpn_type.ask": {
        "de": "Verbindungsart: 1 = OpenVPN (Benutzername und Passwort), 2 = WireGuard (Schlüssel)\nAuswahl",
        "en": "Connection type: 1 = OpenVPN (user name and password), 2 = WireGuard (key)\nChoice",
    },
    "wg_key.ask": {
        "de": "WireGuard: privater Schlüssel (unsichtbar)",
        "en": "WireGuard: private key (invisible)",
    },
    "wg_addresses.ask": {
        "de": "WireGuard: deine Adresse im VPN (z. B. 10.64.222.21/32)",
        "en": "WireGuard: your address in the VPN (for example 10.64.222.21/32)",
    },
    "vpn_countries.ask": {
        "de": "Land des VPN-Servers (z. B. Germany; Enter = beliebig)",
        "en": "Country of the VPN server (for example Germany; Enter = any)",
    },
    "data_root.why": {
        "de": (
            "Hier liegen alle Medien. Wichtig: Download- und Medienordner müssen auf EINEM Laufwerk liegen,\n"
            "damit fertige Dateien ohne Kopieren (Hardlink) einsortiert werden können. Hast du eine eigene\n"
            "Festplatte für Medien, binde sie vorher ein (docs/host-setup.md, Schritt 4) und gib hier ihren\n"
            "Einhängepunkt an. Sonst nimm den Vorschlag."
        ),
        "en": (
            "All media is stored here. Important: the download and media folders must be on ONE drive so\n"
            "finished files can be filed without copying (hardlink). If you have a separate disk for\n"
            "media, mount it first (docs/host-setup.md, step 4) and give its mount point here.\n"
            "Otherwise take the suggestion."
        ),
    },
    "data_root.ask": {"de": "Ordner für die Medien", "en": "Folder for the media"},
    "folders_default.ask": {
        "de": "Ordnernamen: {names}. Diese Namen übernehmen?",
        "en": "Folder names: {names}. Use these names?",
    },
    "folder.ask": {"de": "Ordnername für {kind}", "en": "Folder name for {kind}"},
    "kind.MEDIA_DIR_MOVIES": {"de": "Filme", "en": "movies"},
    "kind.MEDIA_DIR_SERIES": {"de": "Serien", "en": "series"},
    "kind.MEDIA_DIR_AUDIOBOOKS": {"de": "Hörbücher", "en": "audiobooks"},
    "kind.MEDIA_DIR_MUSIC": {"de": "Musik", "en": "music"},
    "kind.MEDIA_DIR_PODCASTS": {"de": "Podcasts", "en": "podcasts"},
    "kind.MEDIA_DIR_YOUTUBE": {"de": "YouTube-Videos", "en": "YouTube videos"},
    "web_user.why": {
        "de": (
            "Die Webseite ist nur aus deinem Heimnetz erreichbar und verlangt trotzdem eine Anmeldung.\n"
            "Das Passwort wird nur als Prüfsumme (Hash) gespeichert, nie im Klartext."
        ),
        "en": (
            "The web page can only be reached from your home network and still asks for a login.\n"
            "The password is only stored as a hash, never in plain text."
        ),
    },
    "web_user.ask": {"de": "Benutzername für die Webseite", "en": "User name for the web page"},
    "web_password.ask": {
        "de": "Passwort für die Webseite (mindestens {n} Zeichen, unsichtbar)",
        "en": "Password for the web page (at least {n} characters, invisible)",
    },
    "web_password2.ask": {"de": "Passwort wiederholen", "en": "Repeat the password"},
    "web_password_change.ask": {
        "de": "Soll das Passwort der Webseite geändert werden?",
        "en": "Change the web page password?",
    },
    "qbit_auto.why": {
        "de": (
            "Der Torrent-Client (qBittorrent) hat eine eigene Oberfläche mit eigenem Passwort. Du brauchst es\n"
            "im Alltag nicht: die Webseite spricht selbst mit ihm. Ein zufälliges, sicheres Passwort ist am besten;\n"
            "es steht nur in der Datei .env."
        ),
        "en": (
            "The torrent client (qBittorrent) has its own interface with its own password. You do not need it\n"
            "in daily use: the web page talks to it itself. A random, strong password is best;\n"
            "it is only kept in the file .env."
        ),
    },
    "qbit_auto.ask": {
        "de": "Soll ein sicheres Passwort für qBittorrent automatisch erzeugt werden?",
        "en": "Generate a strong password for qBittorrent automatically?",
    },
    "qbit_password.ask": {
        "de": "Passwort für qBittorrent (mindestens {n} Zeichen, unsichtbar)",
        "en": "Password for qBittorrent (at least {n} characters, invisible)",
    },
    "lan_confirm.why": {
        "de": (
            "Die Webseite soll nur in deinem Heimnetz antworten. Dafür muss ich die Adresse dieses Computers\n"
            "und dein Netz kennen."
        ),
        "en": (
            "The web page must only answer in your home network. For that I need this computer's address\n"
            "and your network."
        ),
    },
    "lan_confirm.ask": {
        "de": "Dieser Computer hat die Adresse {address} im Netz {subnet}. Stimmt das?",
        "en": "This computer has the address {address} in the network {subnet}. Is that right?",
    },
    "lan_address.ask": {
        "de": "Adresse dieses Computers im Heimnetz (zu finden mit `hostname -I`)",
        "en": "This computer's address in your home network (find it with `hostname -I`)",
    },
    "lan_subnet.ask": {
        "de": "Dein Heimnetz (z. B. 192.168.178.0/24)",
        "en": "Your home network (for example 192.168.178.0/24)",
    },
    "jellyfin_url.why": {
        "de": (
            "Jellyfin zeigt später deine Medien an. Wenn du die Adresse und einen Schlüssel angibst, sagt die\n"
            "Webseite Jellyfin nach jedem Download, dass es neu einlesen soll. Das ist freiwillig: Enter überspringt."
        ),
        "en": (
            "Jellyfin shows your media later. If you give its address and a key, the web page tells Jellyfin to\n"
            "rescan after every download. This is optional: Enter skips it."
        ),
    },
    "jellyfin_url.ask": {
        "de": "Adresse deines Jellyfin (z. B. http://192.168.178.108:8096, Enter = überspringen)",
        "en": "Address of your Jellyfin (for example http://192.168.178.108:8096, Enter = skip)",
    },
    "jellyfin_key.why": {
        "de": 'Den Schlüssel erzeugst du in Jellyfin: Dashboard → API-Schlüssel → "+". Er ist wie ein Passwort.',
        "en": 'You create the key in Jellyfin: Dashboard → API Keys → "+". It is like a password.',
    },
    "jellyfin_key.ask": {
        "de": "Jellyfin-API-Schlüssel (unsichtbar, Enter = später)",
        "en": "Jellyfin API key (invisible, Enter = later)",
    },
    "alert_url.why": {
        "de": (
            "Wenn das VPN ausfällt, kann eine Nachricht an dein Handy gehen, z. B. über einen ntfy-Server in\n"
            "deinem Netz. Freiwillig: Es wird nie etwas an einen fremden Dienst geschickt, den du nicht angibst."
        ),
        "en": (
            "If the VPN drops, a message can be sent to your phone, for example through an ntfy server in\n"
            "your network. Optional: nothing is ever sent to a service you do not name."
        ),
    },
    "alert_url.ask": {
        "de": "Adresse für Warnungen (z. B. http://ntfy.lan/vpn, Enter = keine)",
        "en": "Address for alerts (for example http://ntfy.lan/vpn, Enter = none)",
    },
    "home_prefix.why": {
        "de": (
            "Das ist eine zusätzliche Sicherheitsprüfung: Taucht die öffentliche Adresse deines Internetanschlusses\n"
            "im VPN-Verkehr auf, wird alles angehalten. Bei einem Anschluss mit IPv6 (DS-Lite) ist das das\n"
            'IPv6-Präfix. In der Fritz!Box: Internet → Online-Monitor → "IPv6-Präfix" (z. B. 2003:abcd:1200::/56).\n'
            "Freiwillig, aber empfohlen."
        ),
        "en": (
            "This is an extra safety check: if your internet line's public address ever shows up in the VPN\n"
            "traffic, everything is paused. On a line with IPv6 (DS-Lite) that is the IPv6 prefix.\n"
            'In the Fritz!Box: Internet → Online Monitor → "IPv6 prefix" (for example 2003:abcd:1200::/56).\n'
            "Optional, but recommended."
        ),
    },
    "home_prefix.ask": {
        "de": "Öffentliche Adresse / IPv6-Präfix deines Anschlusses (Enter = überspringen)",
        "en": "Public address / IPv6 prefix of your line (Enter = skip)",
    },
    "timezone.ask": {
        "de": "Zeitzone: {zone}. Stimmt das?",
        "en": "Time zone: {zone}. Is that right?",
    },
    "timezone_name.ask": {
        "de": "Zeitzone (z. B. Europe/Berlin)",
        "en": "Time zone (for example Europe/Berlin)",
    },
    "summary.title": {"de": "Zusammenfassung", "en": "Summary"},
    "summary.secret": {"de": "(gesetzt)", "en": "(set)"},
    "summary.web": {
        "de": "Anmeldung an der Webseite: {user} (Passwort: gesetzt)",
        "en": "Web page login: {user} (password: set)",
    },
    "summary.confirm": {
        "de": "Jetzt installieren? Das lädt Programme herunter und startet die Dienste.",
        "en": "Install now? This downloads programs and starts the services.",
    },
    # ------------------------------------------------------------------ existing installation
    "menu.found": {
        "de": "Es gibt schon eine Installation hier. Was möchtest du tun?",
        "en": "There is already an installation here. What would you like to do?",
    },
    "menu.options": {
        "de": "  1 = Einstellungen ändern\n  2 = Aktualisieren (neue Version holen)\n  3 = Reparieren (alle Schritte noch einmal prüfen)\n  4 = Zustand anzeigen\n  5 = Beenden\nAuswahl",
        "en": "  1 = Change settings\n  2 = Update (fetch the new version)\n  3 = Repair (check all steps again)\n  4 = Show the state\n  5 = Quit\nChoice",
    },
    "menu.resume": {
        "de": 'Eine frühere Installation wurde bei Schritt "{step}" unterbrochen. Es geht dort weiter.',
        "en": 'An earlier installation stopped at the step "{step}". It carries on from there.',
    },
    "menu.change_note": {
        "de": "Die bisherigen Werte stehen in eckigen Klammern. Bei Passwörtern behält Enter den gespeicherten Wert.",
        "en": "The current values are in square brackets. For passwords, Enter keeps the saved value.",
    },
    # ------------------------------------------------------------------ steps
    "step.prepare.title": {
        "de": "Docker und Grundprogramme installieren",
        "en": "Install Docker and the basic programs",
    },
    "step.prepare.hint": {
        "de": "Die Installation von Docker ist fehlgeschlagen. Meist fehlt eine Internetverbindung oder die Uhrzeit ist falsch. Prüfe mit `ping -c 2 deb.debian.org` und starte ./install.sh noch einmal.",
        "en": "Installing Docker failed. Usually there is no internet connection or the clock is wrong. Check with `ping -c 2 deb.debian.org` and run ./install.sh again.",
    },
    "step.config.title": {
        "de": "Einstellungsdatei .env schreiben",
        "en": "Write the settings file .env",
    },
    "step.config.hint": {
        "de": "Die Datei .env konnte nicht geschrieben werden. Prüfe, ob der Ordner dir gehört und Platz auf der Platte ist (`df -h`).",
        "en": "The file .env could not be written. Check that the folder is yours and that the disk has room (`df -h`).",
    },
    "step.folders.title": {
        "de": "Ordner für die Medien anlegen und prüfen",
        "en": "Create and check the media folders",
    },
    "step.folders.hint": {
        "de": "Die Ordner konnten nicht angelegt werden, oder Hardlinks gehen nicht. Der Medienordner muss auf einer Linux-Festplatte (ext4) liegen, nicht auf einer Netzwerkfreigabe. Siehe docs/host-setup.md, Schritt 4.",
        "en": "The folders could not be created, or hardlinks do not work. The media folder must be on a Linux disk (ext4), not on a network share. See docs/host-setup.md, step 4.",
    },
    "step.vpn.title": {
        "de": "VPN-Datei vorbereiten",
        "en": "Prepare the VPN file",
    },
    "step.vpn.hint": {
        "de": "Die VPN-Datei konnte nicht vorbereitet werden. Meist kann der Servername nicht aufgelöst werden (kein Internet) oder die Datei ist keine gültige .ovpn-Datei.",
        "en": "The VPN file could not be prepared. Usually the server name cannot be resolved (no internet) or the file is not a valid .ovpn file.",
    },
    "step.logins.title": {
        "de": "Anmeldungen einrichten (Webseite, qBittorrent, Wächter)",
        "en": "Set up the logins (web page, qBittorrent, watchdog)",
    },
    "step.logins.hint": {
        "de": "Eine Anmeldung konnte nicht eingerichtet werden. Meist läuft Docker noch nicht richtig: `sudo docker run --rm hello-world` sollte funktionieren.",
        "en": "A login could not be set up. Usually Docker is not working yet: `sudo docker run --rm hello-world` should work.",
    },
    "step.start.title": {
        "de": "Dienste bauen und starten, warten bis alles läuft",
        "en": "Build and start the services, wait until everything runs",
    },
    "step.start.hint": {
        "de": "Die Dienste sind nicht alle gesund geworden. Am häufigsten: falsche VPN-Zugangsdaten. Schau mit `sudo docker compose ps` und `sudo docker compose logs gluetun --tail 30` nach (die letzten Zeilen kannst du unbedenklich zeigen; niemals die Datei .env).",
        "en": "Not all services became healthy. Most often: wrong VPN credentials. Look with `sudo docker compose ps` and `sudo docker compose logs gluetun --tail 30` (the last lines are safe to show; never the file .env).",
    },
    "step.qbit.title": {
        "de": "qBittorrent einrichten (Kategorien)",
        "en": "Set up qBittorrent (categories)",
    },
    "step.qbit.hint": {
        "de": "qBittorrent antwortet noch nicht. Warte eine Minute und starte ./install.sh noch einmal.",
        "en": "qBittorrent does not answer yet. Wait a minute and run ./install.sh again.",
    },
    "step.checks.title": {
        "de": "Alles prüfen",
        "en": "Check everything",
    },
    "step.checks.hint": {
        "de": 'Eine Prüfung ist fehlgeschlagen. Die Zeile mit FAIL sagt, was. Danach hilft ./install.sh → "Reparieren".',
        "en": 'A check failed. The line with FAIL says what. After fixing it, ./install.sh → "Repair" helps.',
    },
    "step.vpn.not_needed": {
        "de": "Bei diesem Anbieter ist keine Datei nötig.",
        "en": "No file is needed for this provider.",
    },
    "step.running": {
        "de": "Schritt {n} von {total}: {title}",
        "en": "Step {n} of {total}: {title}",
    },
    "step.skipped": {
        "de": "Schritt {n} von {total}: {title} (schon erledigt)",
        "en": "Step {n} of {total}: {title} (already done)",
    },
    "step.failed": {
        "de": 'Der Schritt "{title}" ist fehlgeschlagen.',
        "en": 'The step "{title}" failed.',
    },
    "step.failed_more": {
        "de": "Was passiert ist, steht in {log} (ohne Passwörter). Wenn du Hilfe holst, zeige nur die letzten Zeilen daraus, niemals die Datei .env.\nDanach ./install.sh noch einmal starten: erledigte Schritte werden übersprungen.",
        "en": "What happened is in {log} (without passwords). If you ask for help, show only the last lines of it, never the file .env.\nThen run ./install.sh again: finished steps are skipped.",
    },
    "wait.waiting": {
        "de": "Warte, bis alle Dienste laufen ({seconds} s) …",
        "en": "Waiting until all services run ({seconds} s) …",
    },
    "wait.timeout": {
        "de": "Nach {seconds} Sekunden laufen noch nicht alle Dienste: {services}.",
        "en": "After {seconds} seconds not all services run yet: {services}.",
    },
    "wait.ok": {"de": "Alle Dienste laufen.", "en": "All services are running."},
    "backup.made": {
        "de": "Die alte Einstellungsdatei wurde gesichert als {name}.",
        "en": "The old settings file was saved as {name}.",
    },
    "env.added": {
        "de": "Neue Einstellungen aus der Vorlage übernommen: {names}.",
        "en": "New settings taken over from the template: {names}.",
    },
    "env.placeholder": {
        "de": "ACHTUNG: Für {names} fehlt noch ein Wert (Datei .env).",
        "en": "ATTENTION: a value is still missing for: {names} (file .env).",
    },
    "checks.proxy_password": {
        "de": "Zum Testen der Anmeldung: Passwort der Webseite noch einmal eingeben (unsichtbar, Enter = Test überspringen)",
        "en": "To test the login: type the web page password once more (invisible, Enter = skip the test)",
    },
    "checks.jellyfin_unreachable": {
        "de": "Hinweis: Jellyfin unter {url} antwortet nicht. Prüfe die Adresse und ob Jellyfin läuft.",
        "en": "Note: Jellyfin at {url} does not answer. Check the address and that Jellyfin is running.",
    },
    "checks.jellyfin_ok": {"de": "Jellyfin antwortet.", "en": "Jellyfin answers."},
    "checks.killswitch": {
        "de": "Optionaler Test der VPN-Sperre: Er hält das VPN kurz an und prüft, dass qBittorrent dann kein Netz hat (etwa 2 Minuten). Jetzt ausführen?",
        "en": "Optional test of the VPN lock: it stops the VPN briefly and checks that qBittorrent then has no network (about 2 minutes). Run it now?",
    },
    # ------------------------------------------------------------------ update, state, done
    "update.start": {"de": "Aktualisierung", "en": "Update"},
    "update.no_install": {
        "de": "Hier ist noch nichts installiert (keine Datei .env). Starte ./install.sh ohne --update.",
        "en": "Nothing is installed here yet (no file .env). Run ./install.sh without --update.",
    },
    "update.backup": {"de": "Erst eine Sicherung anlegen …", "en": "Making a backup first …"},
    "update.backup_failed": {
        "de": "Die Sicherung ist fehlgeschlagen, deshalb wird nichts aktualisiert. Siehe docs/operations.md.",
        "en": "The backup failed, so nothing is updated. See docs/operations.md.",
    },
    "update.pull": {"de": "Neue Version holen …", "en": "Fetching the new version …"},
    "update.pull_failed": {
        "de": "Die neue Version konnte nicht geholt werden (git pull). Hast du Dateien selbst geändert? `git status` zeigt es.",
        "en": "The new version could not be fetched (git pull). Did you change files yourself? `git status` shows it.",
    },
    "update.done": {"de": "Aktualisierung abgeschlossen.", "en": "Update finished."},
    "state.title": {"de": "Zustand", "en": "State"},
    "done.title": {"de": "Fertig!", "en": "Done!"},
    "done.dry": {
        "de": "Testlauf beendet: Es wurde nichts verändert. Ohne --dry-run startest du die echte Installation.",
        "en": "Test run finished: nothing was changed. Without --dry-run you start the real installation.",
    },
    "done.open": {
        "de": 'Öffne im Browser:  {url}\nund melde dich an mit dem Benutzer "{user}" und dem Passwort, das du eben gewählt hast.',
        "en": 'Open in your browser:  {url}\nand log in with the user "{user}" and the password you just chose.',
    },
    "done.qbit": {
        "de": "Die Oberfläche von qBittorrent (selten nötig): {url}",
        "en": "qBittorrent's own interface (rarely needed): {url}",
    },
    "done.backups": {
        "de": "Sicherungen: `sudo make backup` legt sie im Ordner {folder} ab, auf der Medien-Platte (Anleitung: docs/operations.md).",
        "en": "Backups: `sudo make backup` puts them in the folder {folder}, on the media drive (guide: docs/operations.md).",
    },
    "done.help": {
        "de": "Hilfe: Die Datei HILFE-MIT-KI.md kannst du in einen KI-Chat kopieren; die KI erklärt dir dann jeden Schritt. Schicke ihr nie Passwörter oder die Datei .env.",
        "en": "Help: you can paste the file AI-HELP.md into an AI chat; it will then explain every step. Never send it passwords or the file .env.",
    },
    "done.relogin": {
        "de": "Damit `docker` ohne `sudo` geht, melde dich einmal ab und wieder an.",
        "en": "To use `docker` without `sudo`, log out and in again once.",
    },
    # ------------------------------------------------------------------ automatic updates (#42)
    "autodeploy.why": {
        "de": (
            "Automatische Updates: Die App kann sich selbst auf den neuesten Stand bringen. Alle 5 Minuten\n"
            "schaut sie in GitLab nach einer neuen Version. Sie installiert sie nur, wenn die Tests dort grün\n"
            "sind und alles gesund läuft, macht vorher ein Backup und rollt zurück, wenn etwas schiefgeht.\n"
            "Dafür braucht sie ein nur lesendes Zugangs-Token (siehe docs/autodeploy.md)."
        ),
        "en": (
            "Automatic updates: the app can bring itself up to date. Every 5 minutes it looks in GitLab\n"
            "for a new version. It installs it only if the tests there are green and everything is healthy,\n"
            "makes a backup first and rolls back if something goes wrong. It needs a read-only access\n"
            "token for that (see docs/autodeploy.md)."
        ),
    },
    "autodeploy.ask": {
        "de": "Automatische Updates einschalten?",
        "en": "Switch on automatic updates?",
    },
    "autodeploy_remote.ask": {
        "de": "Adresse des Repositorys in GitLab (http://…/gruppe/projekt.git)",
        "en": "Address of the repository in GitLab (http://…/group/project.git)",
    },
    "autodeploy_token.ask": {
        "de": "Zugangs-Token (Rolle Reporter, Rechte read_api und read_repository; wird nicht angezeigt)",
        "en": "Access token (role Reporter, scopes read_api and read_repository; not shown)",
    },
    "err.remote": {
        "de": "Das muss eine Adresse wie http://gitlab.lan/gruppe/projekt.git sein, ohne Benutzer und Passwort darin.",
        "en": "This must be an address like http://gitlab.lan/group/project.git, without a user name or password in it.",
    },
    "err.token": {
        "de": "Das sieht nicht wie ein GitLab-Token aus (8 bis 200 Zeichen: Buchstaben, Ziffern, _ . = -).",
        "en": "This does not look like a GitLab token (8 to 200 characters: letters, digits, _ . = -).",
    },
    "autodeploy.title": {
        "de": "Automatische Updates einrichten",
        "en": "Setting up automatic updates",
    },
    "autodeploy.token_saved": {
        "de": "Das Token liegt jetzt in config/autodeploy/token (nur für dich lesbar).",
        "en": "The token is now in config/autodeploy/token (readable only by you).",
    },
    "autodeploy.token_missing": {
        "de": "Es gibt kein Token. Lege es in config/autodeploy/token ab (Datei nur für dich lesbar) und starte den Assistenten noch einmal. Siehe docs/autodeploy.md.",
        "en": "There is no token. Put it in config/autodeploy/token (file readable only by you) and run the assistant again. See docs/autodeploy.md.",
    },
    "autodeploy.check_failed": {
        "de": "Die Zeitschaltung ist eingerichtet, aber die Prüfung meldet ein Problem (siehe oben). Nach dem Beheben: sudo -u {user} python3 scripts/ops/autodeploy.py check",
        "en": "The timer is set up, but the check reports a problem (see above). After fixing it: sudo -u {user} python3 scripts/ops/autodeploy.py check",
    },
    "autodeploy.on": {
        "de": "Automatische Updates sind an: alle 5 Minuten wird nachgesehen. Pausieren: python3 scripts/ops/autodeploy.py pause",
        "en": "Automatic updates are on: a check every 5 minutes. To pause: python3 scripts/ops/autodeploy.py pause",
    },
    "autodeploy.off": {
        "de": "Automatische Updates sind ausgeschaltet.",
        "en": "Automatic updates are switched off.",
    },
    "qbit.proxy_fix": {
        "de": "qBittorrent bekommt eine Einstellung, damit seine Weboberfläche über den Proxy (Port 8081) erreichbar ist. Es wird dafür kurz angehalten und danach neu gestartet.",
        "en": "qBittorrent gets a setting so that its web page can be reached through the proxy (port 8081). It is stopped briefly for that and started again afterwards.",
    },
    "step.autodeploy.title": {
        "de": "Automatische Updates",
        "en": "Automatic updates",
    },
    "step.autodeploy.hint": {
        "de": "Die automatischen Updates konnten nicht eingerichtet werden. Der Rest ist fertig und läuft. Mehr dazu in docs/autodeploy.md und config/installer.log.",
        "en": "Automatic updates could not be set up. Everything else is finished and running. More in docs/autodeploy.md and config/installer.log.",
    },
    "done.jellyfin": {
        "de": 'In Jellyfin: lege eine Bibliothek pro Ordner an (z. B. {example}). Für Podcasts den Typ "Musik" wählen.',
        "en": 'In Jellyfin: create one library per folder (for example {example}). Choose the type "Music" for podcasts.',
    },
}

PLACEHOLDER_FIELDS = {
    key: {name for _, name, _, _ in string.Formatter().parse(text) if name}
    for key, by_lang in TEXTS.items()
    for text in [by_lang["en"]]
}


def t(lang: str, key: str, **values: object) -> str:
    """The sentence `key` in `lang` ("de" or "en"), with `{name}` fields filled in."""
    text = TEXTS[key][lang if lang in ("de", "en") else "en"]
    return text.format(**values) if values or "{" in text else text
