# IBR Controller

Python controller and CSV logger for reading TESA gauges through an IBR bus and
the vendor-supplied `ibr_ddk.dll`.

## What this version improves

- Continuously spreads gauge reads across low-frequency measurement windows,
  avoiding a large burst of DLL calls immediately before each CSV row.
- Averages valid samples collected during each window and adapts decimal
  precision to the observed spread.
- Uses a preallocated multi-gauge reader to reduce `ctypes` allocation overhead.
- Retains the Windows DLL-directory handle so dependent DLL lookup remains
  active for the lifetime of the controller.
- Pumps Windows messages during initialization and acquisition to reduce driver
  stalls, with a 30-second initialization timeout.
- Uses monotonic time for acquisition deadlines and pacing, so system-clock
  adjustments do not shorten or extend a run.
- Validates sensor, frequency, and duration input and always closes the CSV file
  and deinitializes the device, including initialization and runtime failures.

## Requirements

- Windows and Python 3.9 or newer
- IBR DDK installed at `C:\IBR_DDK\DLL\x64\ibr_ddk.dll`
- An IBR setup file at `C:\IMB_Test\IMB_Test.ddk`

Change `DLL_PATH`, `SETUP_PATH`, and `MODULE_NUMBER` near the top of `main.py`
if your installation differs.

## Run

```powershell
python main.py
```

The interactive prompt accepts sensor lists such as `1,2,6`, ranges such as
`1-3,6`, or `all`. Measurements and logs are written to `Measurements/`.

Intervals of one second or longer use continuous software streaming with
windowed averaging. Faster intervals use adaptive burst oversampling. Both
modes cap sampling at 50 full gauge passes per output interval to avoid
overloading the device.

## Test

The test suite covers parsing, validation, timing calculations, formatting, and
failure cleanup without requiring Windows or physical gauges:

```powershell
python -m unittest discover -s tests -v
```

## Setup-file notes

Known offsets in the IBR setup file:

- 0 (2 bytes): device type
- 2 (2 bytes): device address
- 4 (2 bytes): instrument type
- 6: COM/USB or IMB-LAN serial data
- 30 (2 bytes): connection state for connection 1
- 32 (1 byte): IMBus module type
- 33 (1 byte): IMBus channel number
- 34 (2 bytes): high master
- 36 (2 bytes): low master
- 38 (2 bytes): master for zero adjustment
- 40 (4 bytes): zero offset (`float`)
- 44 (2 bytes): reserved or digital step, depending on the instrument
