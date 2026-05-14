import argparse
import http.client
import sys
import time

IP = "192.168.0.14"
PWD = "sec"
SDA = "31"
SCL = "30"
I2CADDR = 0x62
CRC8_POLYNOMIAL = 0x31
CRC8_INIT = 0xFF


def crc(data, count):
    c = CRC8_INIT
    for i in range(count):
        c ^= data[i]
        for _ in range(8):
            c = ((c << 1) ^ CRC8_POLYNOMIAL) if (c & 0x80) else (c << 1)
    return c & 0xFF


def get_req(ip, cmd, timeout=0.5):
    conn = http.client.HTTPConnection(ip, timeout=timeout)
    try:
        conn.request("GET", cmd)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return body.decode().strip()
    except Exception as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        sys.exit(1)


def i2c_write(ip, pwd, sda, scl, address, reg):
    cmd = f"/{pwd}/?pt={sda}&scl={scl}"
    payload = bytearray([address * 2] + reg)
    get_req(ip, cmd + "&i2c_cmd=1")
    get_req(ip, cmd + "&i2c_cmd=2")
    get_req(ip, cmd + "&i2c_sendp=" + payload.hex().upper())
    get_req(ip, cmd + "&i2c_cmd=3")


def i2c_read_bytes(ip, pwd, sda, scl, address, n):
    cmd = f"/{pwd}/?pt={sda}&scl={scl}"
    read_addr = bytearray([address * 2 | 1])
    get_req(ip, cmd + "&i2c_cmd=1")
    get_req(ip, cmd + "&i2c_cmd=2")
    get_req(ip, cmd + "&i2c_send=" + read_addr.hex().upper())
    result = []
    for i in range(n):
        ack = "1" if i == n - 1 else "0"
        val = get_req(ip, cmd + f"&i2c_read={ack}")
        result.append(int(val, 16))
    get_req(ip, cmd + "&i2c_cmd=3")
    return result


def cmd_read(args):
    i2c_write(args.ip, args.pwd, args.sda, args.scl, I2CADDR, [0xec, 0x05])
    time.sleep(0.5)
    r = i2c_read_bytes(args.ip, args.pwd, args.sda, args.scl, I2CADDR, 9)

    if r[2] != crc(r[0:2], 2):
        print("CO2: CRC error", file=sys.stderr)
    else:
        print(f"CO2:  {(r[0] << 8) | r[1]} ppm")

    if r[5] != crc(r[3:5], 2):
        print("Temp: CRC error", file=sys.stderr)
    else:
        temp = round(-45 + 175 * ((r[3] << 8) | r[4]) / 65535, 2)
        print(f"Temp: {temp} °C")

    if r[8] != crc(r[6:8], 2):
        print("RH:   CRC error", file=sys.stderr)
    else:
        rh = round(100 * ((r[6] << 8) | r[7]) / 65535, 2)
        print(f"RH:   {rh} %")


def cmd_read_temp_offset(args):
    i2c_write(args.ip, args.pwd, args.sda, args.scl, I2CADDR, [0x23, 0x18])
    time.sleep(1)
    r = i2c_read_bytes(args.ip, args.pwd, args.sda, args.scl, I2CADDR, 3)
    if r[2] != crc(r[0:2], 2):
        print("CRC error", file=sys.stderr)
        sys.exit(1)
    offset = round(175 * ((r[0] << 8) | r[1]) / 65535, 2)
    print(f"Temperature offset: {offset} °C")


def cmd_write_temp_offset(args):
    val = args.value
    if val < 0 or val >= 20:
        print("Error: temperature offset must be in range [0, 20)", file=sys.stderr)
        sys.exit(1)
    raw = int(val * 65535 / 175)
    data = list(raw.to_bytes(2, byteorder="big"))
    data.append(crc(data, 2))
    i2c_write(args.ip, args.pwd, args.sda, args.scl, I2CADDR, [0x24, 0x1D] + data)
    print(f"Temperature offset set to {val} °C")


def cmd_read_hum_offset(args):
    i2c_write(args.ip, args.pwd, args.sda, args.scl, I2CADDR, [0x48, 0x01])
    time.sleep(1)
    r = i2c_read_bytes(args.ip, args.pwd, args.sda, args.scl, I2CADDR, 3)
    if r[2] != crc(r[0:2], 2):
        print("CRC error", file=sys.stderr)
        sys.exit(1)
    raw = int.from_bytes(bytes(r[0:2]), byteorder="big", signed=True)
    offset = int(100 * raw / 65535)
    print(f"Humidity offset: {offset} %")


def cmd_write_hum_offset(args):
    val = args.value
    if val < -29 or val > 29:
        print("Error: humidity offset must be in range [-29, 29]", file=sys.stderr)
        sys.exit(1)
    sign = 1 if val > 0 else (-1 if val < 0 else 0)
    raw = int(val * 65535 / 100) + sign
    data = list(raw.to_bytes(2, byteorder="big", signed=True))
    data.append(crc(data, 2))
    i2c_write(args.ip, args.pwd, args.sda, args.scl, I2CADDR, [0x48, 0x02] + data)
    print(f"Humidity offset set to {val} %")


def main():
    parser = argparse.ArgumentParser(description="SCD41 sensor tool via MegaD I2C")
    parser.add_argument("--ip",  default=IP,  help=f"MegaD IP (default: {IP})")
    parser.add_argument("--pwd", default=PWD, help=f"MegaD password (default: {PWD})")
    parser.add_argument("--sda", default=SDA, help=f"SDA pin (default: {SDA})")
    parser.add_argument("--scl", default=SCL, help=f"SCL pin (default: {SCL})")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("read", help="Read CO2, temperature, humidity")
    sub.add_parser("read-temp-offset", help="Read temperature offset")
    sub.add_parser("read-hum-offset",  help="Read humidity offset")

    p = sub.add_parser("write-temp-offset", help="Write temperature offset (°C, 0–20)")
    p.add_argument("value", type=float, help="Offset in °C")

    p = sub.add_parser("write-hum-offset", help="Write humidity offset (%%, -29–29)")
    p.add_argument("value", type=int, help="Offset in %%")

    args = parser.parse_args()

    dispatch = {
        "read":              cmd_read,
        "read-temp-offset":  cmd_read_temp_offset,
        "write-temp-offset": cmd_write_temp_offset,
        "read-hum-offset":   cmd_read_hum_offset,
        "write-hum-offset":  cmd_write_hum_offset,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
