# Example metro controller: user guide

The metro controller manages a small packet network: switches, the links
between them, the hosts attached, and services that join two switches on a
VLAN. This page is the part of its guide that describes its REST API. All
addresses are under `/api` and answer JSON.

## Looking at the network

| Method | Address | What it answers |
|---|---|---|
| GET | /api/health | Whether the controller is up |
| GET | /api/switches | Every switch |
| GET | /api/switches/<id> | One switch |
| GET | /api/links | Every link between switches |
| GET | /api/hosts | Every attached host |
| GET | /api/paths/<src>/<dst> | A route between two switches |

## Services

A service needs a `name`, the switch at each end (`a_end`, `z_end`) and may
carry a `vlan`. Without the first three the controller answers 400.

- `GET /api/services` every service
- `GET /api/services/<name>` one service
- `POST /api/services` create a service
- `PUT /api/services/<name>` change a service
- `DELETE /api/services/<name>` remove a service

```
POST /api/services
{"name": "demo", "a_end": "sw1", "z_end": "sw3", "vlan": 20}
```
