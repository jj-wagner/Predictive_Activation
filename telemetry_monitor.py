import eventlet
eventlet.monkey_patch()

import csv
import time
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib import hub

class PredictiveTelemetryApp(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(PredictiveTelemetryApp, self).__init__(*args, **kwargs)
        self.datapaths = {}
        
        # Format: { (switch_id, port_no): (prev_rx_bytes, prev_tx_bytes, prev_time) }
        self.prev_stats = {} 
        
        # Open the CSV file and write the header row
        self.csv_file = open('telemetry_dataset.csv', 'w', newline='')
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(['timestamp', 'switch_id', 'port_no', 'rx_mbps', 'tx_mbps'])
        
        self.monitor_thread = hub.spawn(self._monitor)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, CONFIG_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.logger.info(f"Registered switch: {datapath.id}")
                self.datapaths[datapath.id] = datapath
        elif ev.state == CONFIG_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.info(f"Unregistered switch: {datapath.id}")
                del self.datapaths[datapath.id]

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            hub.sleep(1)

    def _request_stats(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        current_time = time.time()

        for stat in sorted(body, key=lambda stat: stat.port_no):
            if stat.port_no != ev.msg.datapath.ofproto.OFPP_LOCAL:
                port_key = (dpid, stat.port_no)
                
                rx_mbps = 0.0
                tx_mbps = 0.0

                if port_key in self.prev_stats:
                    prev_rx, prev_tx, prev_time = self.prev_stats[port_key]
                    time_diff = current_time - prev_time
                    
                    if time_diff > 0: # (Bytes diff * 8 bits) / (time difference) / 1,000,000 = Mbps
                        rx_mbps = ((stat.rx_bytes - prev_rx) * 8) / time_diff / 1000000.0
                        tx_mbps = ((stat.tx_bytes - prev_tx) * 8) / time_diff / 1000000.0
                self.prev_stats[port_key] = (stat.rx_bytes, stat.tx_bytes, current_time)

                self.logger.info(f"Switch: {dpid} Port: {stat.port_no} | RX: {rx_mbps:.4f} Mbps | TX: {tx_mbps:.4f} Mbps")                
                self.csv_writer.writerow([current_time, dpid, stat.port_no, round(rx_mbps, 4), round(tx_mbps, 4)])
                self.csv_file.flush() 