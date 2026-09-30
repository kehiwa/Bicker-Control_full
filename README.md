# Bicker-Control Full

Netzwerkfähiger Controller für die Bicker UPSI-2412D. Dieses Projekt baut auf `Bicker-Control_small` auf: Die bisherige Stützungslogik bleibt erhalten und wird um Web-GUI, SNMP, Netzwerkzugriff und fünf konfigurierbare Eingänge erweitert.

**Projektstand:** Architektur und Hardware-Vorauswahl dokumentiert; Firmware und freigegebener Schaltplan sind noch nicht implementiert. Die Komponenten sind Entwurfskandidaten, keine freigegebene Serien-BOM.

## Produktziele

- Gleiche Grundfunktion wie `Bicker-Control_small`: USV-Stützfunktion EIN/AUTO/AUS abhängig von fünf potentialfreien Kontakten.
- Gleichzeitiger Gigabit-Ethernet- und WLAN-Client-Betrieb. Kein Access-Point-Modus.
- RJ45 mit PoE; zusätzlicher 5-30-V-DC-Eingang. Beide Quellen dürfen gleichzeitig angeschlossen sein; jede kann das Gerät allein versorgen.
- Web-GUI über HTTPS für USV-Status, Messwerte, Einstellungen, Eingangszuordnung, Benutzer, Logs und Netzwerk.
- SNMPv2c lesend; SNMPv3 `authPriv` lesend und schreibend, mit denselben Berechtigungsgrenzen wie die Web-API. Traps/Inform und dokumentierte MIBs.
- Keine transparente serielle TCP-Konsole. Der Zugriff erfolgt über dokumentierte Werte und kontrollierte Aktionen.
- Bis zu 1 GiB lokaler Ringpuffer auf eMMC, konfigurierbare Aufzeichnung von Zustandswechseln, Alarmen und/oder Stichproben sowie optionaler externer Syslog-Server.

## Hardware

### Rechenmodul und Netzwerk

Gewählt ist ein Raspberry Pi Compute Module 5 mit 4 GB RAM, 16 GB eMMC und WLAN. Als OS ist Raspberry Pi OS Lite 64-bit, Bookworm oder neuer, mit Kernel 6.12 oder neuer vorgesehen.

Der CM5IO-Referenzschaltplan führt die vier CM5-Ethernet-Differentialpaare direkt zu einem MagJack mit integrierter Magnetik; ein zusätzlicher Ethernet-PHY ist nicht vorgesehen. Für den eigenen RJ45 muss ein MagJack mit geeigneten PoE-Center-Taps und ausreichender Stromfreigabe ausgewählt werden. Die WLAN-Antenne muss im Kunststoffgehäuse frei platziert und darf nicht vom Kühlkörper abgeschirmt werden.

### Versorgung

Beide Eingangswege erzeugen eine geregelte 5,1-V-Schiene. Ein TPS2121-Quellenmux priorisiert den Hilfs-DC-Eingang und verwendet PoE als Reserve. Aus der gemeinsamen 5-V-Schiene wird lokal die 3,3-V-GPIO-Referenz erzeugt und an CM5-Kontakt 78 `GPIO_VREF` geführt. Dadurch versorgt jede Quelle beide benötigten Schienen.

| Teilpfad | Vorauswahl | Entwurfsziel |
|---|---|---|
| PoE | TPS2378 PD-Erkennung/Klassifizierung plus UCC28740-gesteuerter isolierter Flyback | IEEE 802.3at Type 2 / PoE+, 5,1 V, 3 A Zielausgang |
| Hilfs-DC | TPS2663 eFuse/Überspannungsschutz plus LM51772 Buck-Boost mit externen MOSFETs | 5-30 V Eingang, 5,1 V, 3 A Zielausgang |
| Quellenwahl | TPS2121 | Aux priorisiert, PoE als Reserve; Rückstromsperre und automatischer Wechsel |
| GPIO-Referenz | AP3441SHE-7B-Topologie aus CM5IO-Referenz | 3,3 V aus `SYS_5V` |

`SYS_5V` ist vorläufig auf 5 V / 3 A (15 W) ausgelegt. PoE+ bietet bei angenommener 85-%-Wandlung ungefähr 21 W am 5-V-Bus und damit Reserve. 802.3af bleibt kompatibel, liefert nach derselben Annahme aber nur ungefähr 11 W beziehungsweise 2,2 A bei 5 V; volle 3-A-Auslegung ist dort erst nach Lastmessung zugesichert. Für den vollen Leistungsrahmen am 5-V-DC-Eingang ist ein Netzteil mit mindestens 4 A vorgesehen; bei 9-30 V genügt eine Quelle mit mindestens 20 W Nennleistung.

PoE++ / IEEE 802.3bt ist für die erste Version nicht vorgesehen. Der gegenwärtige Funktionsumfang benötigt die zusätzliche Vierpaar-PD-Stufe nicht. Bei höherem gemessenem Bedarf kann sie mit einem Type-3-PD-Entwurf, etwa auf Basis TPS23730, neu bewertet werden.

### USV-Schnittstelle und Eingänge

- Isolierte RS232 über ADI ADM3252E, 38400 Baud, 8N1. UART und separates DTR-/Freigabesignal werden über zwei Treiber- und zwei Empfängerkanäle geführt.
- Der aktuelle Vorschlag nutzt CM5-Kontakte 55/51 für UART TX/RX und GPIO18/Kontakt49 für DTR-Steuerung.
- Fünf potentialfreie Eingänge mit eigener Strombegrenzung, Filterung, Transientenschutz und Optokoppler. Vorgeschlagene GPIOs: GPIO5, GPIO6, GPIO13, GPIO16 und GPIO26.
- Reset-Taste: unter 3 s keine Aktion; 3-10 s Netzwerk-Reset; ab 10 s vollständiger Werksreset. Anzeige der gewählten Stufe über RGB-Status-LED. Aktion erfolgt erst beim Loslassen.
- Gerät ist hinter dem USV-Ausgang versorgt. Vor einem zeitgesteuerten USV-Neustart muss die UPSI den Wiederanlauf selbst speichern, da das CM5 während der Abschaltung ausfallen kann.

Die konkreten Kontakt- und Netznamen stehen in [HARDWARE_CONNECTIONS.md](HARDWARE_CONNECTIONS.md). Die detaillierte Power-Topologie mit Budgets und Prüfbedingungen steht in [POWER_STAGE.md](POWER_STAGE.md).

## Steuerung und Berechtigungen

Jeder Eingang kann vom Admin einer von Root freigegebenen Funktion zugewiesen werden; mehrere Eingänge dürfen dieselbe Funktion auslösen. Vorgesehene Funktionen:

- bestehende Stützungslogik sperren/aktivieren
- ein durch Root begrenztes Maximum-Backup-Time-Profil aktivieren
- USV-Ausgang ausschalten
- USV-Ausgang zeitgesteuert neu starten, aber nur bei frischem Status und erkanntem Netzbetrieb
- Ereignis protokollieren und SNMP-Trap/Inform auslösen

Bei mehreren aktiven Backupzeit-Profilen gilt zunächst die kürzeste angeforderte Stützdauer. Neustart wird bei Batteriebetrieb, Kommunikationsverlust oder veraltetem Status abgelehnt. Ein Shutdown im Batteriebetrieb ist eine separate, explizit von Root freizugebende Aktion.

| Rolle | Berechtigungsmodell |
|---|---|
| Root | Produkt-Superadmin; legt Admin-Rechte, Sicherheitsgrenzen, verfügbare USV-Befehle und Eingangsaktionen fest. Admin-Fähigkeiten werden nach der Kontoanlage explizit freigegeben. |
| Admin | Wird zunächst ohne Fähigkeiten angelegt. Verwaltet danach nur die von Root freigegebenen Geräteeinstellungen und gewährt dem User eine Teilmenge der eigenen Berechtigungen. |
| User | Erhält nur Lese- und Aktionsrechte, die der Admin freigegeben hat |

„Root“ ist eine Anwendungsrolle, kein Linux-Shell-Konto. SSH ist im Produktbetrieb deaktiviert; Zugangsdaten werden gehasht gespeichert. Kein bekannter Standardzugang nach Werksreset: die Erstkonfiguration erfolgt kabelgebunden über Ethernet.

## Softwarearchitektur

- 64-bit Raspberry Pi OS Lite auf eMMC, Bookworm oder neuer, Kernel 6.12 oder neuer.
- **Bereits implementiert:** Längenbasierter Bicker-Framecodec in `src/bicker_control/ups/protocol.py` und asynchroner Serial-Owner in `src/bicker_control/ups/transport.py`. Der Dienst serialisiert Requests über eine Queue; zusammengesetzte Vorgänge wie Parameter-SET plus Read-back laufen atomar. `pyserial` ist der Runtime-Adapter für den Linux-UART.
- **Bereits implementiert:** Status-/Messwertcache in `src/bicker_control/ups/monitor.py`. Status wird im Netzbetrieb alle 5 s, im Batteriebetrieb jede Sekunde gepollt; ein Messwert rotiert je erfolgreichem Zyklus. Nach drei aufeinanderfolgenden Statusfehlern (konfigurierbar) wird die Kommunikation als gestört markiert; Wiederholungen nutzen begrenztes exponentielles Backoff.
- **Bereits implementiert:** kontrollierte Aktionen in `src/bicker_control/ups/commands.py`. Backupzeitprofile werden gegen UPS- und Root-Grenzen geprüft und per Read-back bestätigt. `UpsOutput`-Restart führt unmittelbar davor innerhalb derselben Serial-Queue eine frische Statusabfrage aus und wird ohne Netzstatus abgelehnt. Shutdown auf Batterie ist standardmässig gesperrt und nur über eine explizite Root-Konfiguration freigebbar.
- **Bereits implementiert:** SQLite-Store in `src/bicker_control/state_store.py` für einmaliges Root-Bootstrap, scrypt-Passwortverifier, Root/Admin/User-Rollen mit begrenzter Rechteweitergabe, persistente Settings und hashverkettetes Audit.
- **Bereits implementiert:** gemeinsame `DevicePolicy` und FastAPI-Routen in `src/bicker_control/api.py` für One-time-Bootstrap, Bearer-Login/Logout, Status, Settings, Benutzer/Rechteverwaltung und geschützte UPS-Ausgangsaktionen. SNMP soll dieselbe `DevicePolicy` aufrufen. FastAPI/Uvicorn sind Runtime-Abhängigkeiten; `httpx` gehört zum Testextra.
- **Als Nächstes:** SNMP-AgentX-Adapter und Web-GUI-Frontend an dieselbe `DevicePolicy` anbinden.
- Geplant: gemeinsame Autorisierungsprüfung für Web und SNMP; SQLite zusätzlich für Ringlogs.
- Net-SNMP mit v2c read-only und v3 `authPriv`; SET läuft über die zentrale Policy, nicht als beliebiger serieller Befehl. Standard-UPS-MIB soweit passend plus eigene Bicker-MIB.
- Firewall, Watchdog und limitierte Systemlogs.
- IPv4 DHCP/statisch, WLAN-Station, mDNS, NTP; IPv6 optional. Netzwerk-Reset löscht WLAN-Zugang und setzt Ethernet auf DHCP zurück, ohne Benutzer/USV-Konfiguration zu löschen.
- Logs: konfigurierbare Zustandsänderungen, Alarme und periodische Messwerte. Lokaler Ringpuffer bis 1 GiB, optionaler externer Syslog-Server.

### Entwicklungsinstallation

Das Repository verwendet ein Python-`src`-Layout. Für lokale Entwicklung und Tests:

```sh
python -m pip install --editable .
python -m unittest discover -s tests -v
```

`pyserial` wird über die Projektmetadaten installiert. Für Tests ist keine USV angeschlossen; der Transport wird mit einem Fake-Port geprüft.

### Raspberry-Pi-Service

Für einen manuellen Geräteaufbau kann das Wheel zusammen mit dem mitgelieferten
systemd-Service installiert werden:

```sh
sudo useradd --system --home /var/lib/bicker-control --shell /usr/sbin/nologin bicker-control
sudo usermod --append --groups dialout bicker-control
sudo python3 -m pip install bicker_control_full-<version>-py3-none-any.whl
sudo install -d -m 0750 -o root -g bicker-control /etc/bicker-control
sudo install -m 0640 -o root -g bicker-control packaging/bicker-control.env.example /etc/bicker-control/bicker-control.env
sudo install -m 0644 packaging/systemd/bicker-control.service /etc/systemd/system/bicker-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now bicker-control.service
```

Die serielle Schnittstelle und weitere Laufzeitwerte werden in
`/etc/bicker-control/bicker-control.env` angepasst. Die Datenbank bleibt unter
`/var/lib/bicker-control/state.sqlite3` und wird bei Updates nicht überschrieben.

## Installation und GitHub-Build

Das Zielprodukt wird als **ein ARM64-Debianpaket (`.deb`)** für Raspberry Pi OS Bookworm oder neuer ausgeliefert. Nach Installation des OS soll der Kunde genau ein Paket installieren können, zum Beispiel:

```sh
sudo apt install ./bicker-control-full_<version>_arm64.deb
```

Der Builder für dieses Paket liegt unter `packaging/build-deb.sh` und muss auf
einem ARM64-System ausgeführt werden, damit das gebündelte Python-
Laufzeitverzeichnis zur CM5-Architektur passt:

```sh
bash packaging/build-deb.sh dist
sudo apt install ./dist/bicker-control-full_<version>_arm64.deb
```

Der Builder bündelt die Python-Abhängigkeiten unter `/opt/bicker-control/venv`,
installiert den systemd-Service und legt Benutzer, Konfiguration und persistente
Datenpfade an.

Das finale Paket enthält die Anwendung, sämtliche Python-Laufzeitabhängigkeiten, Webdateien, MIBs, systemd-Dienste, Standardkonfiguration und Datenbankmigrationen. Abhängigkeiten auf OS-Grundkomponenten wie Python 3 und systemd werden als Debian-Abhängigkeiten deklariert. Zugangsdaten und persistente Daten liegen ausserhalb des Pakets unter `/etc/bicker-control` und `/var/lib/bicker-control`, damit Updates die Konfiguration erhalten. Für einen Werksreset werden sie kontrolliert zurückgesetzt.

GitHub Actions prüft bei Push und Pull Request die UPS-Protokolltests und baut ein Python-Wheel als Entwicklungsartefakt. Bei versionierten Tags (`v*`) oder manuellem Workflow-Start wird zusätzlich über QEMU/Buildx ein ARM64-`.deb` gebaut. Der Build bündelt die Python-Laufzeitabhängigkeiten und erzeugt `SHA256SUMS`. Bei einem Versionstag werden Paket und Prüfsumme zusätzlich an das passende GitHub Release angehängt; bei manuellem Start stehen sie als Workflow-Artefakt bereit.

Der CI-Ablauf liegt unter `.github/workflows/test-package.yml`.

## Hardware-Pendenzen

Diese Liste enthält ausschließlich Prüfungen am aufgebauten Gerät. Konstruktions- und Softwareaufgaben sind keine Hardware-Pendenzen und werden separat weiterbearbeitet.

- [ ] CM5 mit PoE+ und Hilfs-DC bei 70 °C Umgebung betreiben: Boot, CPU/eMMC-Last, Ethernet und WLAN gleichzeitig; `SYS_5V`-Stabilität, Stromaufnahme, Temperatur und mögliche Drosselung aufzeichnen.
- [ ] Quellenwahl prüfen: PoE allein, DC allein, beide gemeinsam; DC-Priorität, Ausfall jeder Quelle unter Last, Rückspeisung, Startstrom und Wiederanlauf verifizieren.
- [ ] PoE an af- und at-PSEs testen: Erkennung/Klassifizierung, T2P-Status, 5-V-Ausgangsleistung und Verhalten bei Lastspitzen. af-Vollleistung nur freigeben, wenn gemessene Last mit Reserve innerhalb des verfügbaren Budgets bleibt.
- [ ] 5-30-V-Eingang an 5 V / 4 A sowie repräsentativen 9 V, 12 V, 24 V und 30 V Quellen prüfen; Verpolung, Einschaltstrom, Überspannungs-/Surge-Reaktion und thermische Stabilität testen.
- [ ] Flyback prüfen: Regelung bei 5,1 V / 3 A, Start/Leerlauf, Kurzschluss, Überlast, PoE-Klassengrenzen, Sekundärtemperatur, EMI und galvanische Isolation.
- [ ] UPSI-RS232 messen: tatsächliche DB9-Sender-/Empfängerpins 2/3/4/6, DTR-Freigabe und Pegel; Pin 1-5-Brücke und DB9-Pin 8 nicht als Gerätespeisung verwenden.
- [ ] UPSI-Protokoll am echten Gerät prüfen: Parameter 0x02 lesen/schreiben/Read-back, Neustart-/Shutdown-Befehl und Verzögerung, Speicherung über Stromverlust sowie Verhalten bei Statusverlust.
- [ ] UPS-Ausgangsneustart nur bei Netzbetrieb erproben: bestätigen, dass die UPSI den Wiederanlauf autonom ausführt, wenn das CM5 beim Ausschalten selbst stromlos wird.
- [ ] Fünf Kontakteingänge mit realen Kontaktleitungen prüfen: NO/NC, Prellen, Verzögerung, Störfestigkeit, Kabellänge, Kontaktstrom und gleichzeitig aktive Profile.
- [ ] 3-s-/10-s-Tastenablauf, LED-Rückmeldung, Netzwerk-Reset und vollständigen Werksreset am Gerät prüfen; kurzen unbeabsichtigten Druck ohne Reset bestätigen.
- [ ] Ethernet/MagJack, PoE-Center-Taps, ESD/Surge und WLAN-Empfang im Kunststoffgehäuse inklusive finaler Antennen-/Kühlkörperposition testen.
- [ ] Dauerlauf- und Stromunterbrechungstest der eMMC-Logs durchführen; Ringpuffergrenze, Datenbankintegrität und Wiederherstellung nach Brownout prüfen.
- [ ] EMV/ESD-, PoE-Interoperabilitäts- und thermische Prüfungen im finalen Gehäuse durchführen.

## Dokumente

- [ARCHITECTURE.md](ARCHITECTURE.md): Architektur, Software, Berechtigungen und getroffene Produktentscheidungen.
- [HARDWARE_CONNECTIONS.md](HARDWARE_CONNECTIONS.md): CM5-Kontakte, GPIO-Belegung und EasyEDA-Netzplan.
- [POWER_STAGE.md](POWER_STAGE.md): Versorgungszweige, Wandler/Mux-Kandidaten, Leistungsbudget und Hardware-Prüfungen.
- `de_user_manual_upsi-2412d.pdf` und `en_user_manual_ups-gen2-configuration-software.pdf`: bereitgestellte Bicker-Handbücher.
- CM5IO-Schaltplanreferenz: offizielle Raspberry Pi Revision-2 KiCad-Dateien, Link in den Hardware-Dokumenten.