#!/usr/bin/env python

# import normal packages
import platform
import logging
import os
import sys

if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests  # for http GET
import configparser  # for config/ini file

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusEvccChargerService:
    def __init__(self, servicename, paths, productname='EVCC-Charger', connection='EVCC REST API'):
        config = self._getConfig()
        deviceinstance = int(config['DEFAULT']['Deviceinstance'])
        lpInstance = int(config['DEFAULT']['LoadpointInstance'])

        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        paths_wo_unit = [
            '/Status',
            '/Mode'
        ]

        # get data from go-eCharger
        data = self._getEvccChargerData()
        result = data["result"]
        loadpoint = result["loadpoints"][lpInstance]

        # Set custom name from loadpoint title
        customname = str(loadpoint['title'])

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion',
                                   'Unkown version, and running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)  #
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', customname)
        #self._dbusservice.add_path('/FirmwareVersion', int(data['divert_update']))
        self._dbusservice.add_path('/HardwareVersion', 2)
        #self._dbusservice.add_path('/Serial', data['comm_success'])
        self._dbusservice.add_path('/Connected', 1)
        self._dbusservice.add_path('/UpdateIndex', 0)

        self._dbusservice.add_path('/Position', 1) # 0: ac out, 1: ac in

        # add paths without units
        for path in paths_wo_unit:
            self._dbusservice.add_path(path, None)

        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path, settings['initial'], gettextcallback=settings['textformat'], writeable=False)

        # last update
        self._lastUpdate = 0

        # charging time in float
        self._chargingTime = 0.0

        # add _update function 'timer'
        gobject.timeout_add(2000, self._update)  # pause 2sec before the next request

        # add _signOfLife 'timer' to get feedback in log every 5minutes
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config

    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config['DEFAULT']['SignOfLifeLog']

        if not value:
            value = 0

        return int(value)

    def _getEvccChargerStatusUrl(self):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']

        if accessType == 'OnPremise':
            URL = "http://%s/api/state" % (config['ONPREMISE']['Host'])
        else:
            raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

        return URL

    def _getEvccChargerData(self):
        URL = self._getEvccChargerStatusUrl()
        request_data = requests.get(url=URL)

        # check for response
        if not request_data:
            raise ConnectionError("No response from EVCC-Charger - %s" % (URL))

        json_data = request_data.json()

        # check for Json
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        return json_data

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
        logging.info("--- End: sign of life ---")
        return True

    def _update(self):
        try:
            # get data from go-eCharger
            data = self._getEvccChargerData()
            result = data["result"]
            loadpoint = result["loadpoints"][lpInstance]

            # send data to DBus

            # not really needed, but can be enabled
            voltage = 230 # adjust to your voltage
            self._dbusservice['/Ac/L1/Power'] = float(loadpoint['chargeCurrents'][0]) * voltage # watt
            self._dbusservice['/Ac/L2/Power'] = float(loadpoint['chargeCurrents'][1]) * voltage # watt
            self._dbusservice['/Ac/L3/Power'] = float(loadpoint['chargeCurrents'][2]) * voltage # watt
            self._dbusservice['/Ac/Voltage'] = voltage

            self._dbusservice['/Ac/Power'] = float(loadpoint['chargePower']) # w
            self._dbusservice['/Current'] = float(loadpoint['chargeCurrent'])

            self._dbusservice['/SetCurrent'] = float(loadpoint['chargeCurrent'])
            self._dbusservice['/MaxCurrent'] = int(loadpoint['maxCurrent']) # int(data['ama'])


            # 0: Manual, 1: Auto, 2: Scheduled
            if "pv" in loadpoint["mode"]:
                self._dbusservice['/Mode'] = 1
                self._dbusservice['/StartStop'] = 1
            elif loadpoint["mode"] == "off":
                self._dbusservice['/Mode'] = 0
                self._dbusservice['/StartStop'] = 0
            else:
                self._dbusservice['/Mode'] = 0
                self._dbusservice['/StartStop'] = 1

	        # 0:EVdisconnected; 1:Connected; 2:Charging; 3:Charged; 4:Wait sun; 5:Wait RFID; 6:Wait enable; 7:Low SOC; 8:Ground error; 9:Welded contacts error; defaut:Unknown;
            status = 0
            if loadpoint['connected'] == False:
                status = 0
            elif loadpoint['connected'] == True:
                if loadpoint['charging'] == False:
                    status = 1
                else:
                    status = 2
            self._dbusservice['/Status'] = status

            # is this session charged energy or total charged energy?
            if status == 0:
                self._dbusservice['/Ac/Energy/Forward'] = 0
            else:
                self._dbusservice['/Ac/Energy/Forward'] = float(loadpoint['chargedEnergy']) / 1000  # kWh

            if status == 0:
                self._dbusservice['/ChargingTime'] = 0
            else:
                self._dbusservice['/ChargingTime'] = int(loadpoint["chargeDuration"])/1000000000  # s

            # logging
            logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
            logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
            logging.debug("---")

            # increment UpdateIndex - to show that new data is available
            index = self._dbusservice['/UpdateIndex'] + 1  # increment index
            if index > 255:  # maximum value of the index
                index = 0  # overflow from 255 to 0
            self._dbusservice['/UpdateIndex'] = index

            # update lastupdate vars
            self._lastUpdate = time.time()
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)

        # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True


def main():
    # configure logging
    logging.basicConfig(format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        level=logging.INFO,
                        handlers=[
                            logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                            logging.StreamHandler()
                        ])

    try:
        logging.info("Start")

        from dbus.mainloop.glib import DBusGMainLoop
        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # formatting
        _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
        _a = lambda p, v: (str(round(v, 1)) + 'A')
        _w = lambda p, v: (str(round(v, 1)) + 'W')
        _v = lambda p, v: (str(round(v, 1)) + 'V')
        #_degC = lambda p, v: (str(v) + '°C')
        _s = lambda p, v: (str(v) + 's')

        # start our main-service
        pvac_output = DbusEvccChargerService(
            servicename='com.victronenergy.evcharger',
            paths={
                '/Ac/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
                '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
                '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
                '/ChargingTime': {'initial': 0, 'textformat': _s},
                '/Ac/Voltage': {'initial': 0, 'textformat': _v},
                '/Current': {'initial': 0, 'textformat': _a},
                '/SetCurrent': {'initial': 0, 'textformat': _a},
                '/MaxCurrent': {'initial': 0, 'textformat': _a},
                #'/MCU/Temperature': {'initial': 0, 'textformat': _degC},
                '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))}
            }
        )

        logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
        mainloop = gobject.MainLoop()
        mainloop.run()
    except Exception as e:
        logging.critical('Error at %s', 'main', exc_info=e)


if __name__ == "__main__":
    main()
