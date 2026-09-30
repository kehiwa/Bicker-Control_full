# Power Stage Design Note

Status: preferred topology and component candidates for the EasyEDA power sheet. This is not a released converter design: transformer, switching FETs, magnetics, compensation, protection thresholds, and thermal results require calculation and prototype validation.

## Design contract

- `SYS_5V`: 5 V nominal, 3 A continuous design envelope (15 W). Derive `+3V3_IO` locally after source selection so PoE and auxiliary DC each supply both system rails.
- PoE: IEEE 802.3at Type 2 / PoE+ is the full-load target. Remain detectable/compatible with 802.3af, but do not guarantee the complete 3 A bus envelope from af until worst-case load is measured.
- Auxiliary DC: 5-30 V nominal input. Specify at least 20 W available at the source for the full 15 W output envelope. A 5 V input requires about 3.5 A calculated at 85% efficiency, so specify a 5 V / 4 A source for full capability.
- Both sources may be connected together; auxiliary DC is preferred, PoE is standby. Reverse current between sources must be blocked. A brief reboot is acceptable, although the selected mux supports seamless changeover.
- Ambient can reach 70 C. All power parts, magnetics, connectors, and capacitors require derating and thermal review at that ambient.

## Preferred topology

```text
RJ45 PoE pairs
  -> PoE-rated MagJack center taps
  -> pair-set bridge rectifiers + surge protection
  -> TPS2378 PD detection/classification/hot-swap (PoE+ Type 2)
  -> raw `POE_PRI` DC bus
  -> UCC28740-controlled isolated flyback, transformer, secondary rectifier
  -> `P5V_POE` = 5.1 V nominal, 3 A design target
                                      \
                                       -> TPS2121 priority mux -> `SYS_5V`
                                      /                         -> CM5 contacts 77,79,81,83,85,87
5-30 V terminal                     /                          -> carrier 5 V loads
  -> fuse + reverse-polarity protection
  -> TPS2663 eFuse (UVLO/OVLO/current limit)
  -> LM51772 buck-boost controller + external MOSFETs/inductor
  -> `P5V_AUX` = 5.1 V nominal, 3 A design target

`SYS_5V` -> AP3441SHE-7B-class 3.3 V buck -> `+3V3_IO`
`+3V3_IO` -> CM5 GPIO_VREF contact 78 and 3.3 V logic
```

The TPS2378EVM-105 schematic is a PoE PD front-end reference. Its output is an unregulated PoE bus; it is not a regulated 5 V supply. It demonstrates the PD bridge, detection/classification, hot-swap, and adapter-priority circuit, but the product still needs a separately designed isolated DC/DC converter.

The preferred 802.3at PoE DC/DC is a flyback using UCC28740 with optocoupler feedback and secondary rectification. The controller is rated -40 to +125 C and supports a regulated CV/CC flyback. Use the TPS2378 `T2P` indication through an optocoupler to firmware so it can distinguish a Type 2 PSE from an af source and log/limit operation appropriately. Confirm `T2P` polarity and sink-current limits from the TPS2378 datasheet before choosing the optocoupler LED resistor.

## Component candidates

| Function | Part / topology | Verified envelope / reason | Remaining design work |
|---|---|---|---|
| PoE PD | TI TPS2378 | IEEE 802.3at Type 2, af fallback, 100 V internal hot-swap FET, 850 mA operating current, -40 to +85 C | Pair-set bridges, classification resistor, TVS, bulk capacitance, T2P interface, ambient/junction thermal check |
| PoE flyback controller | TI UCC28740 | CV/CC flyback controller with optocoupler feedback, -40 to +125 C | Choose switching frequency, turns ratio, primary MOSFET, current sense, secondary rectifier, optocoupler/TL431 compensation, transformer insulation and EMI network |
| DC input eFuse | TI TPS2663 family | 4.5-60 V input, 0.6-6 A adjustable current, 31 mOhm typical, -40 to +125 C; industrial surge/current/power limiting features | Select exact suffix for reverse-polarity option; set UVLO/OVLO, current limit, inrush and fault retry; coordinate external TVS clamp |
| DC buck-boost controller | TI LM51772 | 3.5-55 V input, four-switch synchronous buck-boost controller, -40 to +125 C | Size four MOSFETs, inductor, input/output caps and current sense for 5.1 V / 3 A at 5 V, 30 V, and 70 C |
| Source mux | TI TPS2121 | 2.7-22 V, 4.5 A device maximum, 56 mOhm typical path, reverse-current block and automatic/seamless switchover, -40 to +125 C | Set IN1 as auxiliary priority, IN2 as PoE backup; validate continuous 3 A dissipation and output voltage at hot/cold corners |
| 3.3 V GPIO buck | CM5IO reference BOM AP3441SHE-7B | Reusing the CM5IO rail topology reduces carrier risk | Confirm exact output/current rating and 70 C derating against GPIO_VREF plus peripheral load |
| System power monitor | INA226-class I2C high-side monitor (optional but recommended) | Reports `SYS_5V` voltage/current for web status, SNMP, and rail alarms | Select shunt value, Kelvin routing, address, and current range; monitor each input separately only if diagnostics require it |

## Power budget

Use the CM5IO-recommended 5 V / 3 A case as the first product rail envelope because this carrier does not use USB power, HDMI, PCIe, or M.2 loads. It is a board capability target, not a claim that the CM5 consumes 15 W continuously.

| Condition | Calculation | Design implication |
|---|---|---|
| System output | 5.0 V x 3.0 A = 15 W | Continuous converter and mux target; separately verify CM5 transient current |
| PoE+ Type 2 | ~25 W at PD x 0.85 assumed efficiency = ~21 W | Approximately 6 W provisional margin over the bus envelope |
| PoE af | ~13 W at PD x 0.85 = ~11 W | Approximately 2.2 A at 5 V; permit operation only if worst-case measured device load fits with margin |
| Auxiliary input at 5 V | 15 W / 0.85 = 17.6 W, or 3.53 A | 5 V / 4 A minimum source for full bus envelope |
| Auxiliary input at 30 V | 17.6 W / 30 V = 0.59 A | Use a source rated at least 20 W after startup/transient margin |
| TPS2121 path loss | 3 A x 56 mOhm = 0.168 V; I^2R = 0.50 W typical | Verify actual maximum RDS(on), copper area, airflow/heat spreading, and output voltage at 70 C |
| TPS2663 path loss | 3.53 A x 31 mOhm = 0.109 V; I^2R = 0.39 W typical | Check hot RDS(on), current-limit tolerance, and thermal pad/copper conditions |

The 85% conversion efficiency is an engineering estimate used to reserve power, not a guaranteed specification. Measure both converters at load and 70 C. The TPS2121 mux and TPS2663 eFuse each dissipate heat at high current; consider external-FET ideal-diode/mux stages if temperature-rise tests show inadequate margin.

## Power and control signals

| Signal | Route / purpose |
|---|---|
| `POE_T2P_PRI` | TPS2378 Type 2 PSE indication on the PoE primary-side domain; drive an optocoupler input with a current-limited network after confirming polarity/electrical limits |
| `POE_TYPE2_SYS` | Optocoupler transistor output on the isolated secondary/system side to a CM5 GPIO with `+3V3_IO` pull-up; firmware records available PoE class |
| `PWR_AUX_PG` | TPS2663 power-good/fault output to a CM5 GPIO, if enabled in chosen suffix/configuration |
| `PWR_MUX_PG` | TPS2121 power-good output to CM5 GPIO; indicates `SYS_5V` availability |
| `SYS_5V_SENSE` | INA226/shunt sense pair at the mux output for bus voltage/current history and SNMP diagnostics |
| `P5V_POE`, `P5V_AUX`, `SYS_5V`, `+3V3_IO` | Dedicated test points for bring-up, source switchover, load, and thermal tests |

The TPS2378 `T2P` pin is referenced to the PoE primary return. Do not connect it directly to `GND_SYS` or a CM5 GPIO; use the optocoupler barrier shown above. Confirm `T2P` polarity and sink-current limits from the datasheet before choosing the LED resistor.

On af, firmware must log the lower advertised power budget; it must not repeatedly restart or enable a feature that would overdraw the source. Since the UPS controller itself is downstream of the UPS output, rail shutdown remains an expected outcome if the UPS output is deliberately switched off.

## PoE++ decision

Do not require PoE++ / IEEE 802.3bt for the first product revision. The current 15 W bus target fits within an 802.3at Type 2 budget after conversion, and PoE+ is more common in installed network switches. Keep the CM5 Ethernet data path four-pair capable, but populate a two-pair 802.3at PD stage. If measured system load grows above the PoE+ budget, evaluate TI TPS23730 (IEEE 802.3bt Type 3 PD with integrated flyback/active-clamp-forward PWM controller, -40 to +125 C) as a higher-power alternative. Its reference design still requires external magnetics, switching FETs, rectification, and a reviewed PoE center-tap network.

## Release gates

- TPS2378 EVM schematic copied into EasyEDA as the PoE PD front-end reference; do not treat its raw PD bus as a 5 V output.
- Flyback transformer and UCC28740 loop designed for PoE input range, 5.1 V/3 A, short-circuit, open-load, PSE class behavior, and insulation requirements.
- LM51772 calculator/reference-design work completed at VIN=5 V and 30 V, VOUT=5.1 V, full load, and hot ambient.
- TPS2663 OVLO and TVS/clamp chosen from actual DC source transient profile; 30 V continuous must not nuisance trip.
- TPS2121 priority, current limit, inrush, reverse-current blocking, hot RDS(on), and source-fail transition verified on a bench.
- 70 C chamber test covers cold start, CM5 boot/storage writes, Ethernet/Wi-Fi activity, simultaneous source connection, and full-load rail limits.