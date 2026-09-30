# EasyEDA Hardware Connection Plan

Status: electrical block and net-name proposal, not yet a fabrication-ready schematic. Raspberry Pi CM5IO revision 2 KiCad design (updated 2025-10-06) is the carrier reference. Exact CM5 connector pad numbers and the complete power design must be copied from the official CM5 datasheet/reference schematic into EasyEDA before layout. Detailed power topology and budget: [POWER_STAGE.md](POWER_STAGE.md).

## CM5 and Ethernet

Use the Raspberry Pi Compute Module 5, 4 GB RAM, 16 GB eMMC, Wi-Fi. The official CM5IO reference design connects the CM5's Ethernet interface as four differential pairs directly to the Ethernet magnetics/MagJack; it does not place a separate DP83867-class PHY on the carrier. Therefore, do not add an external Ethernet PHY.

| CM5 reference net | Carrier connection |
|---|---|
| CM5 contact 12 / 10 | `Ethernet_Pair0_P` / `Ethernet_Pair0_N` to matching MagJack pair |
| CM5 contact 4 / 6 | `Ethernet_Pair1_P` / `Ethernet_Pair1_N` to matching MagJack pair |
| CM5 contact 11 / 9 | `Ethernet_Pair2_P` / `Ethernet_Pair2_N` to matching MagJack pair |
| CM5 contact 3 / 5 | `Ethernet_Pair3_P` / `Ethernet_Pair3_N` to matching MagJack pair |
| `ETH_LED0/1` (exact CM5 names to verify) | MagJack LEDs through current-limit resistors, if exposed and desired |

Use a Gigabit MagJack with integrated magnetics, common-mode chokes, and accessible PoE center taps on the cable-side windings. The Raspberry Pi reference BOM uses TRJG0926HENL, but do not reuse it for PoE until its exact center-tap pins and PoE current rating are confirmed. Route all four pairs with controlled differential impedance and matched lengths per CM5IO/layout guidance. Add ESD protection at the connector that is suitable for Gigabit Ethernet and does not violate the pair capacitance budget.

RJ45 assignment:

- Cable-side transformer interface goes only to the RJ45 contacts and PoE extraction/ESD network.
- Module-side transformer interface goes only to the four CM5 Ethernet pair nets.
- PoE extraction connects to the specified transformer center taps through the rectifier/PD network; never inject DC into the differential data pins.
- Ethernet shield/chassis strategy is separate from logic ground. Provide a chassis/shield net and define its RC/ground connection after enclosure and EMC review; do not automatically short RJ45 shield to CM5 ground.

These contact numbers are the module pad numbers from Raspberry Pi's official CM5IO revision 2 KiCad symbol, not the pin numbers of a 40-pin GPIO header.

## CM5 power and GPIO contact assignments

The official symbol marks six module contacts as `+5v_(Input)` and a dedicated GPIO voltage reference. Connect all indicated supply contacts and ground contacts; do not leave power pins floating.

| CM5 contact | Reference signal | Carrier net |
|---|---|---|
| 77, 79, 81, 83, 85, 87 | `+5v_(Input)` | `SYS_5V` |
| 78 | `GPIO_VREF(1.8v/3.3v_Input)` | `+3V3_IO` (use 3.3 V mode) |
| All CM5 pins named `GND` | `GND` | `GND_SYS` |

Generate `+3V3_IO` from `SYS_5V` with a carrier regulator sized for GPIO reference and all 3.3 V peripherals. Follow the CM5IO voltage-reference arrangement; do not tie `GPIO_VREF` to 1.8 V while using 3.3 V logic peripherals. The CM5 module 5 V input is separate from this GPIO voltage reference.

### Candidate GPIO mapping

The following GPIO-to-CM5-contact numbers come from the same official CM5 module symbol. This is a practical first assignment; disable the serial console on UART0 in the OS before using it for the UPS.

| Function | BCM GPIO | CM5 module contact | Electrical direction |
|---|---:|---:|---|
| UPS UART TX | GPIO14 | 55 | CM5 to ADM3252E logic-side TX input |
| UPS UART RX | GPIO15 | 51 | ADM3252E logic-side RX output to CM5 |
| DTR/enable control | GPIO18 | 49 | CM5 to second ADM3252E driver input |
| Input 1 | GPIO5 | 34 | Optocoupler logic output to CM5 |
| Input 2 | GPIO6 | 30 | Optocoupler logic output to CM5 |
| Input 3 | GPIO13 | 28 | Optocoupler logic output to CM5 |
| Input 4 | GPIO16 | 29 | Optocoupler logic output to CM5 |
| Input 5 | GPIO26 | 24 | Optocoupler logic output to CM5 |
| Reset button | GPIO17 | 50 | Active-low switch input to CM5 |
| Status LED red | GPIO22 | 46 | Output through current-limit resistor/driver |
| Status LED green | GPIO23 | 47 | Output through current-limit resistor/driver |
| Status LED blue | GPIO24 | 45 | Output through current-limit resistor/driver |
| PoE Type-2 status | GPIO27 | 48 | Optocoupler output; keep TPS2378 `T2P` on PoE primary side |

Confirm these pins against the chosen CM5IO symbol revision and actual module ordering code when building the EasyEDA symbol. If we add a fan, HAT identification EEPROM, or another carrier peripheral, reserve its GPIOs before locking this assignment.

## Power tree

Both source inputs may be present at once and each must operate the product on its own. Allow a brief reboot during changeover. Power the CM5 from a regulated 5 V rail following the CM5IO design; do not power it from UPS DB9 pin 8.

```text
RJ45 cable pairs
  -> PoE-rated MagJack center taps
  -> polarity bridge / TPS2378 IEEE 802.3af/at PD front end
  -> raw isolated-design primary PD bus `POE_PRI`
  -> UCC28740 flyback controller + transformer/secondary rectifier
  -> output P5V_POE
                                      \
                                       -> TPS2121 priority power mux
                                      /   -> SYS_5V -> CM5 5 V input
5-30 V DC terminal                   /              -> carrier peripherals
  -> fuse + reverse-polarity block
  -> TPS2663 eFuse / surge-current protection
  -> LM51772 buck-boost, output P5V_AUX

SYS_5V -> AP3441SHE-7B-class 3.3 V buck -> +3V3_IO -> CM5 GPIO_VREF and logic
```

| Net | Source / connection | Requirements |
|---|---|---|
| `POE_PRI` | TPS2378 VDD/RTN PD bus | Raw negotiated PoE voltage; primary-side domain only, not a user/system supply |
| `P5V_POE` | UCC28740-controlled isolated flyback output | 5.1 V nominal, 3 A target when Type 2 power is available; af operation is limited by negotiated input power and must be validated against actual load |
| `P5V_AUX` | LM51772 output after TPS2663 protection from `VIN_AUX` | 5.1 V nominal candidate over 5-30 V input; validate 5 V boost-mode thermal/current capability |
| `SYS_5V` | TPS2121 output, auxiliary input preferred | 5 V nominal target bus, 3 A continuous design envelope, current-limited and thermally verified |
| `+3V3_IO` | AP3441SHE-7B-class carrier buck from `SYS_5V` | CM5 contact 78 `GPIO_VREF` in 3.3 V mode and 3.3 V-side logic; AP3441SHE-7B appears in the CM5IO reference BOM |
| `GND_SYS` | System logic return | CM5, Ethernet/PHY-side digital reference and non-isolated peripherals |
| `CHASSIS` | RJ45 shield and enclosure bonding domain | EMC-defined connection to system return; not assumed identical to `GND_SYS` |
| `GND_UPS_ISO` | Isolated RS-232/UPS-side return | DB9 pin 5 and ADM3252E isolated field side only; no direct short to `GND_SYS` |
| `V5_INPUT` | 5-30 V screw terminal positive | Fuse, reverse polarity protection, TVS and filter precede DC/DC |
| `GND_INPUT` | 5-30 V screw terminal return | DC/DC input return; tie to system ground only as specified by selected isolation topology |

The CM5 module symbol identifies contacts 77, 79, 81, 83, 85, and 87 as `+5v_(Input)`; connect all six to `SYS_5V`. Contact 78 is `GPIO_VREF(1.8v/3.3v_Input)` and must receive the carrier's 3.3 V logic reference for the GPIO mapping below. Connect all CM5 `GND` contacts to `GND_SYS` as in the official carrier reference. Both PoE and auxiliary DC must independently feed `SYS_5V`; the 3.3 V rail is generated after source selection so it is present regardless of which input is active.

Configure the TPS2121 for `P5V_AUX` priority and `P5V_POE` backup. It supports two inputs from 2.7-22 V, up to 4.5 A, reverse-current blocking, and automatic/seamless switchover. At 3 A, its typical 56 mOhm path resistance implies about 0.17 V drop and 0.5 W dissipation; use the data-sheet maximum resistance and the 70 C board temperature for final thermal validation. The user permits a reboot during source change, but this mux should avoid it when the selected converter remains in regulation. Add test points at `VIN_AUX`, each converter output, `SYS_5V`, `+3V3_IO`, and ground.

The TPS2378 `T2P` indication is referenced to the PoE primary-side return. Route it only through an optocoupler to the CM5 side. Verify its logic polarity/current limit and the selected optocoupler CTR before choosing the input resistor and GPIO pull-up.

### Preliminary rail budget

| Case | Available / calculated input power | Preliminary interpretation |
|---|---:|---|
| `SYS_5V` design envelope | 5 V x 3 A = 15 W | Target continuous carrier capability, including 3.3 V rail conversion |
| 802.3at Type 2 PoE | About 25 W at PD; about 21 W at 85% conversion efficiency | Enough for the 15 W rail target with provisional margin |
| 802.3af PoE | About 13 W at PD; about 11 W at 85% conversion efficiency | About 2.2 A at 5 V; full 3 A envelope not guaranteed. Measure actual worst-case CM5 demand before claiming af operation |
| 5 V auxiliary input | About 17.6 W input for 15 W output at 85% efficiency | At least 3.5 A calculated; specify a 5 V / 4 A source for the full envelope |
| 30 V auxiliary input | About 17.6 W input for 15 W output at 85% efficiency | About 0.6 A calculated, before design margin |

Efficiency is an estimate, not a component guarantee. At 70 C ambient, converter switching loss, inductor temperature rise, TPS2121 conduction loss, and CM5 load peaks must be measured. A common 5 V / 3 A USB adapter is allowed as an auxiliary source but is not specified to provide the full 15 W rail envelope; use a higher-current 5 V source or 9-30 V industrial supply when full margin is required.

### PoE standard decision

Use IEEE 802.3at Type 2 / PoE+ for the first revision. The 15 W system-bus target is below the available PoE+ power after conversion losses, while 802.3af cannot guarantee the full 3 A envelope. PoE++ / IEEE 802.3bt is not needed for the current CM5-only workload and would require a four-pair PD front end and more thermal/EMC work. Revisit PoE++ if later measured load exceeds the PoE+ budget or high-power peripherals are added; a Type 3/4 PD controller such as TPS2372 would then be evaluated.

### Auxiliary power candidates

| Ref. function | Candidate | Design condition |
|---|---|---|
| Input eFuse | TI TPS2663 family | 4.5-60 V input, 6 A class, -40 to +125 C. Verify exact variant's reverse-polarity behavior; retain external fuse/TVS and set UVLO/OVLO/current limit for the actual 5-30 V source envelope |
| Buck-boost controller | TI LM51772 | 3.5-55 V input, -40 to +125 C, with external MOSFETs and inductor. Design for 5.1 V at 3 A over the input range |
| Source mux | TI TPS2121 | 2.7-22 V inputs, up to 4.5 A, -40 to +125 C; both inputs are regulated 5.1 V rails |
| 3.3 V GPIO buck | AP3441SHE-7B topology used by CM5IO | Confirm exact part rating/current and thermal margin for GPIO_VREF plus all 3.3 V peripherals |

TPS2663's 4.5 V minimum input covers a nominal 5 V auxiliary source while LM51772 continues regulating through the eFuse drop. At VIN=5 V the converter runs in boost mode and draws the highest input current; 5 V / 4 A is the preliminary source requirement for the full 15 W bus envelope. At VIN=30 V the current is much lower, but coordinate eFuse OVLO and the external transient clamp so the LM51772 stays below its 55 V operating maximum.

## UPSI-2412D RS-232

Use an ADI ADM3252E isolated dual-channel RS-232 transceiver candidate. Its second transmitter permits a separately controlled DTR/enable line. Preserve isolation between the CM5 and the UPS serial cable.

| Signal / net | Connection |
|---|---|
| `CM5_UART_TX` | CM5 3.3 V UART TX to ADM3252E logic-side driver input for RS-232 TXD |
| `CM5_UART_RX` | ADM3252E logic-side receiver output from RS-232 RXD to CM5 3.3 V UART RX |
| `CM5_UPS_DTR_N` | CM5 GPIO to second ADM3252E driver; output polarity/configuration must match verified UPS enable requirement |
| `DB9_USV_TXD` (USV to controller) | UPS DB9 pin 2 in the current small-project wiring; connect to ADM receiver input |
| `DB9_USV_RXD` (controller to USV) | UPS DB9 pin 3 in the current small-project wiring; connect from ADM transmitter output |
| `DB9_DTR_ENABLE` | Current small-project wiring uses DB9 pin 4 with a continuously active RS-232 level; verify whether pin 4 or pin 6 is the actual UPS input before connecting |
| `DB9_GND` | DB9 pin 5 to `GND_UPS_ISO` |
| DB9 pin 1 | Bridge to DB9 pin 5 as required to enable the UPS DB9 pin 8 auxiliary output; pin 8 itself is otherwise not used to power this controller |
| DB9 pins 7, 9 | No connect unless the UPS manual and measurements establish another use |

Set UART to 38400 baud, 8 data bits, no parity, 1 stop bit. The MAX3232 design in Bicker-Control_small is replaced by the isolated transceiver. Confirm exact ADM3252E logic-side pin numbers and recommended bypass capacitors from its data sheet. Do not connect the UPS RS-232 ground directly to `GND_SYS`.

## Five dry-contact inputs

Provide five two-terminal field input pairs: `IN1_A/IN1_B` through `IN5_A/IN5_B`. Each input receives its own series/current-limit resistor, transient clamp, RC filtering, and optocoupler channel. The five channels may share an isolated field supply and field return but remain isolated from `GND_SYS`. The contact is potential-free; do not expect the customer to provide voltage.

```text
Isolated field supply
  -> series resistor / input protection
  -> terminal INx_A
  -> external dry contact
  -> terminal INx_B
  -> optocoupler LED return

Optocoupler transistor side -> CM5 3.3 V GPIO with defined pull-up/down
```

Initial functional assignments are software-only: existing inhibit/enable control, root-defined Maximum Backup Time profiles, UPS output off, timed UPS restart (mains-only), alarm/log/trap, or no action. Admin can assign the root-authorized functions; root bounds durations and available actions. A closed contact is edge-triggered and rate-limited. Root/admin config must not permit restart when status is stale or battery operation is active.

Input current and protection ratings remain open until the expected field wiring length and desired contact wetting current are selected. Choose input LED current high enough for noise immunity but low enough for long-term contact life.

## Reset button, status LED, and RTC

| Net | Connection |
|---|---|
| `RESET_BUTTON_N` | Sealed momentary switch to `GND_SYS`; external pull-up and ESD protection to 3.3 V |
| `STATUS_LED_R/G/B` | CM5 GPIOs through per-channel resistors to RGB LED; common anode/cathode topology selected to suit GPIO current budget |
| `I2C_SCL`, `I2C_SDA` | Optional battery-backed RTC; 3.3 V pull-ups, address/pin choice reserved |
| `RTC_VBAT` | Coin-cell holder or approved battery input, reverse/charge behavior matched to selected RTC |

Reset timing is handled by a monotonic timer in software; no action under 3 s, network reset for 3-10 s, full factory reset for 10 s or longer. Show stage with the status LED and commit on release. The full reset must not restore a published default password.

## CM5 connector and GPIO mapping work still required

The official CM5IO project splits its schematic into CM5 GPIO and high-speed sheets. In EasyEDA:

1. Import the CM5 module symbol and copy the exact Ethernet pair, UART, GPIO, power, boot, and eMMC-programming pad numbers from the official CM5 pinout/reference schematic.
2. Mark unused high-speed interfaces (HDMI, DSI/CSI, PCIe, USB) explicitly no-connect unless used for manufacturing/debug.
3. Reserve one UART for UPS RS-232 and at least six GPIOs for five inputs plus reset button; use an I2C GPIO expander if GPIO availability is constrained by required boot/debug pins.
4. Keep eMMC/boot pins and CM5 required strap/control nets exactly as the reference design specifies.
5. Copy the reference high-speed Ethernet and CM5 connector routing constraints rather than drawing generic wires through the module footprint.

## BOM candidates and source references

| Item | Candidate | Note |
|---|---|---|
| Compute module | Raspberry Pi CM5, 4 GB / 16 GB eMMC / Wi-Fi | Verify exact order code and temperature rating |
| Gigabit MagJack | TRJG0926HENL used by CM5IO reference BOM | Confirm PoE center taps and current rating before use with PoE |
| PoE PD | TPS2378 | 802.3af/at Type 2 detection/classification/hot-swap front end; its raw PD bus is not regulated 5 V |
| PoE flyback | UCC28740 with optocoupler feedback | CV/CC flyback controller; transformer, MOSFET, rectifier, compensation and EMI values require design/validation |
| Isolated RS-232 | ADM3252E | Two drivers/two receivers cover UART and DTR; verify package assembly and exact pin wiring |
| DC input protection / converter | TPS2663 eFuse + LM51772 buck-boost, with external fuse/reverse-polarity and TVS/EMI network | 60 V eFuse and 55 V controller ratings; set clamp/OVLO from the real transient envelope |
| Power mux | TI TPS2121 | Auxiliary-priority switching between the two regulated 5.1 V rails; validate thermal loss at 3 A and 70 C |
| Field inputs | Optocoupler channels with isolated low-voltage wetting supply | Select parts after cable/input-current requirements |

Official CM5IO revision 2 KiCad design archive: https://pip-assets.raspberrypi.com/categories/1098-design-files/documents/RP-008099-DD-1-CM5%20IO%20Board,%20revision%202,%20KiCAD%20files..zip

Reference naming found in the CM5IO high-speed schematic: `Ethernet_Pair0_P/N` through `Ethernet_Pair3_P/N`. The CM5IO BOM lists the MagJack but no separate Ethernet PHY.