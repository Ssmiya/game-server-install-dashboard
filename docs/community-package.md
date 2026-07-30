# GameDeck 社区游戏适配包

适配包是一个最大 10MB 的 ZIP，所有文件必须位于 ZIP 根目录。包内不能包含
脚本、程序或服务端二进制文件。

## 文件结构

```text
my-game.zip
├── game.json
├── icon.png
└── background.jpg
```

## game.json 示例

```json
{
  "schemaVersion": 1,
  "id": "example-server",
  "name": "Example Dedicated Server",
  "shortName": "EX",
  "description": "通过 SteamCMD 安装的社区游戏服务端示例。",
  "accent": "#4f9cff",
  "tags": ["SteamCMD", "社区"],
  "assets": {
    "icon": "icon.png",
    "background": "background.jpg"
  },
  "adapter": {
    "type": "steamcmd",
    "appId": 123456,
    "executable": "server.x86_64",
    "configName": "server.properties",
    "launchArgs": ["--port", "{{server-port}}"],
    "portField": "server-port",
    "capacityField": "max-players"
  },
  "fields": [
    {
      "key": "server-port",
      "label": "游戏端口",
      "type": "number",
      "value": 27015,
      "min": 1,
      "max": 65535,
      "step": 1,
      "group": "网络",
      "description": "玩家连接服务端使用的端口。"
    },
    {
      "key": "max-players",
      "label": "最大玩家数",
      "type": "number",
      "value": 16,
      "min": 1,
      "max": 128,
      "step": 1,
      "group": "基础设置"
    }
  ],
  "defaultConfig": "server-port=27015\nmax-players=16\n"
}
```

`launchArgs` 中可以使用 `{{字段键名}}` 引用配置表单值。每个参数会作为独立的
进程参数传入，不经过 Shell 解释。

目前只支持匿名登录可下载的 Linux SteamCMD Dedicated Server App。
