# GPIO 17 Monitor

A small web page that polls Raspberry Pi BCM GPIO 17 every 500 ms. The page turns green for HIGH and red for LOW. GPIO 17 is configured as an input with an internal pull-up, so it reads HIGH while idle and LOW when the input is connected to GND.

## Run on Raspberry Pi OS

1. Copy this folder into `/var/www/html` (or clone the repository there).
2. Install GPIO Zero if it is not already available:

   ```sh
   sudo apt update
   sudo apt install python3-gpiozero
   ```

3. Run the server as a user allowed to access GPIO:

   ```sh
   cd /var/www/html
   python3 app.py
   ```

4. Open `http://<raspberry-pi-address>:8000` from a device on the same network. Stop the server with Ctrl+C.

The included Python server serves both the web page and `/api/status`, so no Apache or web-server configuration is needed. It listens on port 8000. If the `gpio` group exists and your user does not have GPIO access, add the user to it and log out/in before starting the server:

```sh
sudo usermod -aG gpio "$USER"
```

## Wiring and safety

Use BCM GPIO 17 (physical header pin 11) as the input. Connect the input to GND to make the reading LOW; leave it disconnected for the internal pull-up to hold it HIGH. Raspberry Pi GPIO pins are **3.3 V only**—never apply 5 V to GPIO 17.

If the page reports that GPIO is unavailable, check that it is running on the Pi, `python3-gpiozero` is installed, and the account running `app.py` has GPIO access.
