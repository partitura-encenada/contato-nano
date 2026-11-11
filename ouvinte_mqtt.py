import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import serial
from serial.tools import list_ports
import paho.mqtt.client as mqtt

CONFIG_FILE = Path(__file__).with_name('nano_config.json')
BAUDRATE = 115200
STATUS_INTERVAL_S = 5
REQUIRED_KEYS = ('server_ip', 'mqtt_port', 'username', 'password', 'short_name')


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(f'Falta o arquivo {CONFIG_FILE.name}: copie o nano_config.example.json e preencha.')
    config = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    missing = [k for k in REQUIRED_KEYS if not config.get(k)]
    unfilled = [k for k in REQUIRED_KEYS if 'COLE_AQUI' in str(config.get(k, ''))]
    if missing or unfilled:
        sys.exit(f'Preencha no {CONFIG_FILE.name}: {", ".join(missing + unfilled)}')
    return config


def is_ouvinte(device):
    """Abre a porta e espera a resposta ID/OUVINTE (ao abrir, o ESP32 reinicia e também se anuncia)."""
    try:
        with serial.Serial(port=device, baudrate=BAUDRATE, timeout=0.5) as s:
            time.sleep(1.5)
            s.reset_input_buffer()
            s.write(b'ID?\n')
            end = time.time() + 3
            while time.time() < end:
                if s.readline().decode('utf-8', errors='ignore').strip() == 'ID/OUVINTE':
                    return True
    except (serial.SerialException, OSError):
        pass
    return False


def find_ouvinte():
    for port in list_ports.comports():
        description = (port.description or '').lower()
        if 'bluetooth' in description or 'bth' in (port.hwid or '').lower():
            continue
        print(f'Testando {port.device}...')
        if is_ouvinte(port.device):
            return port.device
    return None


def parse_line(line):
    """'5/14/2690/0' -> (5, 14, 2690, 0); outras linhas (ID/OUVINTE, lixo de boot) -> None."""
    parts = line.split('/')
    if len(parts) != 4:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def make_client(config):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'contato-ouvinte-{uuid.uuid4().hex[:6]}')
    client.username_pw_set(config['username'], config['password'])
    client.on_connect = lambda c, u, f, rc, p: print(f'MQTT: conectado ({rc})')
    client.on_disconnect = lambda c, u, f, rc, p: print(f'MQTT: desconectado ({rc}), tentando de novo...')
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    # connect_async + loop_start: conecta e reconecta em segundo plano, sem travar a leitura
    client.connect_async(config['server_ip'], int(config['mqtt_port']), keepalive=30)
    client.loop_start()
    return client


def main():
    parser = argparse.ArgumentParser(description='Envia as leituras do ouvinte do Contato ao servidor do NANO.')
    parser.add_argument('--port', help='porta COM do ESP32 ouvinte (ex.: COM7); sem ela, procura sozinho')
    args = parser.parse_args()

    config = load_config()
    prefix = config['short_name'].strip('/')

    port = args.port or find_ouvinte()
    if not port:
        sys.exit('Ouvinte não encontrado. Confira se o ESP32 está ligado e com o ouvinte.cpp gravado.')
    print(f'Ouvinte em {port}. Publicando em {prefix}/equip<N>/(gyro|acc|touch). Ctrl+C para sair.')

    client = make_client(config)
    sent = skipped = 0
    last_status = time.time()

    try:
        with serial.Serial(port=port, baudrate=BAUDRATE, timeout=0.5) as s:
            while True:
                reading = parse_line(s.readline().decode('utf-8', errors='ignore').strip())
                if reading:
                    equip, gyro, acc, touch = reading
                    if client.is_connected():
                        base = f'{prefix}/equip{equip}'
                        client.publish(f'{base}/gyro', gyro)
                        client.publish(f'{base}/acc', acc)
                        client.publish(f'{base}/touch', touch)
                        sent += 1
                    else:
                        skipped += 1   # sem internet: dado ao vivo, descarta em vez de acumular

                if time.time() - last_status >= STATUS_INTERVAL_S:
                    print(f'{sent} leituras enviadas, {skipped} descartadas (sem conexão) nos últimos {STATUS_INTERVAL_S} s')
                    sent = skipped = 0
                    last_status = time.time()
    except KeyboardInterrupt:
        print('\nEncerrando...')
    except (serial.SerialException, OSError) as e:
        print(f'\nConexão com o ouvinte perdida: {type(e).__name__}')
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == '__main__':
    main()
