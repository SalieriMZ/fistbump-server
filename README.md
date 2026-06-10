# 3sx-fistbump-server

Self-host matchmaking server para [crowded-street/3sx](https://github.com/crowded-street/3sx).

Implementa el protocolo Fistbump (TCP signaling + UDP NAT punch) para parear dos clientes 3sx que después corren rollback P2P vía GekkoNet.

## Status

- [x] Fase 1 — recon protocolo
- [x] Fase 2 — server stub mínimo
- [x] Fase 3 — pairing real
- [x] Fase 4 — patch cliente preparado (`src/main.c`)
- [ ] Fase 4 — test localhost (espera build cliente)
- [ ] Fase 5 — LAN
- [ ] Fase 6 — WAN / NAT
- [x] Fase 7 — hardening (janitor, Dockerfile, systemd)

## Archivos

| File | Qué hace |
|------|----------|
| `server.py` | Server asyncio TCP+UDP |
| `test_client.py` | Cliente Python simulado (1 instancia) |
| `test_decline.py` | Test flow DECLINE → CANCEL |
| `client.patch` | Diff cliente para apuntar matchmaking |
| `Dockerfile` | Container deploy |
| `fistbump.service` | Systemd unit |
| `deploy.sh` | Deploy a upstream server VPS |
| `PROTOCOL.md` | Spec completa protocolo |
| `PHASES.md` | Plan implementación |

## Quick start (local)

### Server

```bash
python server.py --tcp-port 9000 --udp-port 9001 -v
```

### Cliente patch

Ya aplicado a `src/main.c` en repo 3sx local. Setea matchmaking IP=127.0.0.1, port=9000 al boot.

Override runtime:
```bash
FISTBUMP_HOST=192.168.1.10 FISTBUMP_PORT=9000 ./3sx.exe
```

Override build-time:
```bash
cmake -B build -DCMAKE_BUILD_TYPE=Debug \
    -DCMAKE_C_FLAGS="-DFISTBUMP_HOST=\\\"matchmaking.example.com\\\" -DFISTBUMP_PORT=9000"
```

### Test sin cliente real

```bash
# Terminal 1
python server.py -v

# Terminal 2
python test_client.py --name A

# Terminal 3
python test_client.py --name B
```

Esperado: ambos progresan hasta `🎮 GAME START — peer=...`.

## Deploy upstream server

```bash
./deploy.sh
```

Abrir en consola AWS upstream server:
- TCP 9000 (signaling)
- UDP 9001 (NAT punch)

## Docker

```bash
docker build -t fistbump .
docker run -d --name fistbump \
    -p 9000:9000/tcp \
    -p 9001:9001/udp \
    --restart=unless-stopped \
    fistbump
```

## Protocolo

Ver [PROTOCOL.md](PROTOCOL.md) para spec completa.

Resumen:
- TCP 9000: HELLO/QUEUE/MATCH/START signaling
- UDP 9001: `<sid> <match_id>` NAT punch + endpoint discovery
- Server captura `(IP_pub, port_pub)` del datagrama UDP → manda en `START` al peer
- Client reusa el socket UDP de Fistbump como socket GekkoNet → NAT mapping preservado

## Limitaciones

- **Sin auth real**: server stub omite OAuth device-grant (DAG flow). Username generado aleatoriamente.
- **Sin TURN relay**: NAT simétrico → falla. Workaround: port-forward UDP del puerto efímero (difícil) o usar Tailscale.
- **No persistencia**: queue + matches en memoria. Restart = pierde estado.
- **Sin moderación**: cualquiera puede conectar.

## Roadmap

Ver [PHASES.md](PHASES.md).
