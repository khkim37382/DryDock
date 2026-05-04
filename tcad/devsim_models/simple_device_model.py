"""
Placeholder for future real DEVSIM device models.

The current project uses generate_tcad_lookup_table.py to create a
TCAD-inspired lookup table. Later, this file can hold actual DEVSIM
mesh/device setup code for a diode, MOS capacitor, MOSFET, ADC proxy,
or RF front-end proxy.

For now, this file exists to show the intended architecture:

satellite radiation/thermal inputs
    -> semiconductor/device model
    -> degradation outputs
    -> lookup table
    -> live satellite sensor fusion
"""