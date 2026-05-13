#!/usr/bin/bash

Usage="Usage: "$0" [-a address] [-p port]"

SAN_IP='^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'
SAN_Port='^((6553[0-5])|(655[0-2][0-9])|(65[0-4][0-9]{2})|(6[0-4][0-9]{3})|([1-5][0-9]{4})|([0-5]{0,5})|([0-9]{1,4}))$'

Scan_IP="192.168.1.1"
Scan_Port="1900"

#if [ "$#" = "0" ];then
#       nmap -sU -p 1900 --script=upnp-info 192.168.1.1
if [ "$#" = "2" ];then
	if [ "$1" = "-a" ] && [[ "$2" =~ $SAN_IP ]]; then
		Scan_IP=$2
	elif [ "$1" = "-p" ] && [[ "$2" =~ $SAN_Port ]]; then
		Scan_Port=$2
	else
		echo "Invalid Arguments!"
		echo $Usage
		exit 1

	fi
elif [ "$#" = "4" ];then
	if [ "$1" = "-a" ] && [[ "$2" =~ $SAN_IP ]]; then
		Scan_IP=$2
	elif [ "$1" = "-p" ] && [[ "$2" =~ $SAN_Port ]]; then
		Scan_Port=$2
	else
		echo "Invalid Arguments!"
		echo $Usage
		exit 2
	fi
	if [ "$3" = "-a" ] && [[ "$4" =~ $SAN_IP ]]; then
		Scan_IP=$4
	elif [ "$3" = "-p" ] && [[ "$4" =~ $SAN_Port ]]; then
		Scan_Port=$4
	else
		echo "Invalid Arguments!"
		echo $Usage
		exit 2
	fi
elif ! [ "$#" = "0" ];then
	echo "Invalid Arguments!"
	echo $Usage
	exit 3
fi

nmap -sU -p $Scan_Port --script=upnp-info $Scan_IP
