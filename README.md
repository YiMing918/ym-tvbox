# ym-tvbox

个人专属 TVBox / 影视仓影视接口。多源聚合，每日自动探活合并，永久地址直达。

## 配置地址

```
https://raw.githubusercontent.com/YiMing918/ym-tvbox/main/peter_tvbox_pro.json
```

国内网络拉取缓慢时可用加速地址：

```
https://gh-proxy.com/https://raw.githubusercontent.com/YiMing918/ym-tvbox/main/peter_tvbox_pro.json
```

在影视仓 / TVBox 类应用的「配置地址」中填入以上任意一条即可。

## 特性

- **220 个站点**：15 个实测可用的采集站优先，其余精选爬虫源均绑定原作者 Jar，确保解析逻辑可正常加载
- **40 条解析线路 + 8 个直播源**
- **每日自动更新**：GitHub Actions 于北京时间每日 05:30 自动抓取上游、两轮探活、重新合并，有变化才提交
- **安全熔断**：当上游大面积失效（可用源或合并站点数低于阈值）时自动放弃提交，保留上一份可用配置

## 上游来源

本配置聚合以下公开配置源，并在此致谢原作者：

- TVboxorg 官方聚合
- 肥猫（经 Lightconer 镜像）
- 王二小放牛娃
- 摸鱼儿（4K）
- 欧歌多仓

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `peter_tvbox_pro.json` | 主配置（自动生成，请勿手动编辑） |
| `merge.py` | 抓取、探活与合并脚本 |
| `.github/workflows/update.yml` | 每日定时更新任务 |

## 免责声明

本仓库仅用于个人学习与技术研究，内容聚合自公开网络接口，版权归原站点及权利方所有。如有侵权，请联系删除。
