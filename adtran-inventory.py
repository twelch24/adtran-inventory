#!/usr/bin/python3

import logging
import os
import requests
import signal
import time
import threading
import xlsxwriter
from datetime import datetime
from queue import Queue
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from netmiko import ConnectHandler as ch

# STFU about insecurity
# not needed if your ssl is truly valid from the machine running the script
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# number of simultaneous ssh connections
num_threads = 25

# This sets up the queue
enclosure_queue = Queue()
# Set up thread lock so that only one thread outputs at a time
lock = threading.Lock()

failures = []
devices = []
start = datetime.now()
inventory = {}
row = 1
col = 0

# create this with librenms under settings/api
auth_token = ''
# this is your librenms URL. https://sitename/api/v0
librenms_api = ''

# list of librenms groups to fetch devices from
groups = ['Adtran']
# groups = ['testbatch']

# need i explain this one?
creds = {
    "username": "",
    "password": "",
    "device_type": "adtran_os",
    "conn_timeout": 20,
}

logging.basicConfig(filename='output.log', level=logging.INFO)

req_headers = {
    'X-Auth-Token': auth_token
}


def fetch_device(device):
    """
    Takes a device_id and fetches information from librenms
    API to populate new_device, returns a dictionary new_device
    """
    new_device = requests.get(
        librenms_api + '/devices/' + str(device),
        headers=req_headers,
        verify=False,
    ).json()['devices']
    if len(new_device) != 1:
        print(f"Error - No such device {device}.")
    return new_device[0]  # we should only return one device


def polldevice(i, q):
    '''
    Takes a task number and device object, populates
    the inventory list of dicts
    '''
    global inventory
    while True:
        dev = q.get()
        creds['host'] = dev['hostname']
        with lock:
            logging.info("Loading: %s (%s)", dev['sysName'], creds['host'])
            print(f"Loading: {dev['sysName']} ({creds['host']})")
        try:
            try:
                net_connect = ch(**creds)
            except NetMikoTimeoutException:
                failures.append(dev)
                with lock:
                    print("Timeout connecting to {dev['sysName']} Count: {len(failures)}")
                logging.warning("Timeout connection to device: %s, Count: %s", dev['sysName'], len(failures))
                q.task_done()
            except NetMikoAuthenticationException:
                failures.append(dev)
                with lock:
                    print("\n{}: ERROR: Authentication failed for {}. Stopping script. \n".format(i, dev['hostname']))
                logging.warning("Authentcation error on device: %s, Count: %s", dev['sysName'], len(failures))
                q.task_done()
                os.kill(os.getpid(), signal.SIGUSR1)

            result_raw = net_connect.send_command('enable', expect_string='#')
            # we overwrite result_raw, but we don't care about the enable output anyway
            result_raw = net_connect.send_command('sho system inventory', expect_string='#')
            net_connect.disconnect()
            for z in result_raw.splitlines():
                # we ignore these because adtran helpfully prints every slot twice
                if "1/" in z:
                    if '....' in z:
                        logging.info('Skipping: %s', z)
                        continue
                    elif 'Alarm' in z:
                        logging.info('Skipping: %s', z)
                        continue
                    elif 'SLOT' in z:
                        logging.info('Skipping: %s', z)
                        continue
                    elif 'Minor' in z:
                        logging.info('Skipping: %s', z)
                        continue
                    elif 'Major' in z:
                        logging.info('Skipping: %s', z)
                        continue
                    elif 'Alert' in z:
                        logging.info('Skipping: %s', z)
                        continue
                    result = z.split(', ')
                    inventory[dev['device_id']].append({
                        "sysName": dev['sysName'].strip(),
                        "ip": dev['hostname'].strip(),
                        "pn": result[0][4:].replace('*', '').strip(),  # partnumber - remove *
                        "type": result[1].strip(),  # part type
                        "slot": result[0][:4].strip(),  # slot
                        "rev": result[2].strip(),  # hardware revision
                        "clei": result[3].strip(),  # clei
                        "serial": result[4].strip(),  # serial number
                    })

            logging.info('Added: %s', dev['sysName'])
            q.task_done()
        except Exception as e:
            with lock:
                print(f"Fail: {dev['sysName']} Count: {len(failures)} Exception: {e}")
            failures.append(dev)
            logging.warning("Fail: %s, Count: %s", dev['sysName'], len(failures))
            q.task_done()


for group in groups:
    print("Fetching devices for group: ", group)
    # fetch all device_ids for group 'group'
    devicegroups = requests.get(
        librenms_api + '/devicegroups/' + str(group),
        headers=req_headers,
        verify=False,
    ).json()['devices']
    # add all those devices to devices
    for dev in devicegroups:
        # create a dict entry inventory['device_id'] and make it
        # a list to hold inventory items later
        inventory[dev['device_id']] = list()
        logging.info("created %s", dev['device_id'])
    print(f'fetched {len(inventory)} devices.')

for i in range(num_threads):
    # Create the thread using 'polldevice' as the function, passing in
    # the thread number and queue object as parameters
    thread = threading.Thread(target=polldevice, args=(i, enclosure_queue,))
    # Set the thread as a background daemon/job
    thread.setDaemon(True)
    # Start the thread
    thread.start()

# For each device add to queue
for dev in inventory:
    target = fetch_device(dev)
    logging.info("Fetched details for %s", str(dev))
    if 'adtran' in target['os'] and 'TA5' in target['sysDescr']:
        enclosure_queue.put(target)
    else:
        print(f"Skipping host {target['sysName']} desc {target['sysDescr']}")

# Wait for all tasks in the queue to be marked as completed (task_done)
enclosure_queue.join()

# set up the xlsx sheet
w = xlsxwriter.Workbook('adtran-inventory.xlsx')  # will be overwritten
inv = w.add_worksheet('inventory')
inv.set_column('A:A', 45)  # sysname
inv.set_column('B:B', 15)  # hostname (IP)
inv.set_column('C:C', 8)   # slot
inv.set_column('D:D', 15)  # partnum
inv.set_column('E:E', 15)  # type
inv.set_column('F:F', 8)   # rev
inv.set_column('G:G', 16)  # clei
inv.set_column('H:H', 25)  # serial
inv.autofilter('A1:H1')
inv.write('A1', 'sysname')
inv.write('B1', 'hostname')
inv.write('C1', 'slot')
inv.write('D1', 'PartNumber')
inv.write('E1', 'Type')
inv.write('F1', 'rev')
inv.write('G1', 'CLEI')
inv.write('H1', 'serial')

# write our spreadsheet data
for d in inventory:
    for item in inventory[d]:
        # print(f"Item {item}")
        try:
            inv.write(row, col, item['sysName'])     # device sysName from Librenms
            inv.write(row, col + 1, item['ip'])      # ip address (maps to librenms hostname)
            inv.write(row, col + 2, item['slot'])    # slot 1/a
            inv.write(row, col + 3, item['pn'])      # part number
            inv.write(row, col + 4, item['type'])    # part type
            inv.write(row, col + 5, item['rev'])     # hardware revision
            inv.write(row, col + 6, item['clei'])    # clei
            inv.write(row, col + 7, item['serial'])  # serial number
        except IndexError:
            print(f"Error writing {dev['sysName']} to spreadsheet.")
w.close()

print("complete.")
end = datetime.now()
print(f"Time: {(end-start)/60}")

if len(failures) > 0:
    logging.info("These devices failed: ")
    for f in failures:
        logging.info('%s hostname %s', f['sysName'], f['hostname'])
        print(f"Failed to download {f['sysName']} {f['hostname']}.")
else:
    print("Yay! No failures!")
