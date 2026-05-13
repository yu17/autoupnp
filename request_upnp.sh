#!/usr/bin/bash

#nmap -sU -p 1900 --script=upnp-info 192.168.0.1

#comm_upnpc="upnpc -u http://192.168.0.1:56688/Public_UPNP_gatedesc.xml"
comm_upnpc="upnpc -u http://192.168.0.1:1900/fqxbs/rootDesc.xml"

#portlist=(30020 30021 60010 60020 60022 60030 60042 60050 60060 60061 60062 60080 60081 60082 60083 60090)
portlist=(60010 60020 60022 60023 60024 60030 60042 60050 60060 60061 60062 60080 60081 60082 60083 60090 30030)

comm_final="$comm_upnpc -r"
for port in ${portlist[@]}; do
	comm_final="$comm_final $port tcp"
	comm_final="$comm_final $port udp"
done

eval $comm_final
