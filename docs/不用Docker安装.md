# 不用 Docker，直接装

给 Mac、Linux，和想用自己电脑上的 Python 的人。只在一台 Windows 上自己用的话，
下 [Windows 包](https://github.com/Esther7314/Loci-Brain/releases/latest) 更省事，什么都不用装。

这套步骤在 Windows 上照着走过一遍；Mac 和 Linux 上是同样的命令，还没在真机上走过。

## 先要有

- **Python 3.12**。别的版本没试过。
- **Node.js 18 或更新**。网关要用它。
- **git**。没有的话，去 GitHub 页面下 zip 解压也行。

## 装

```bash
git clone https://github.com/Esther7314/Loci-Brain.git
cd Loci-Brain
python3.12 -m venv .venv
```

进虚拟环境：

- Mac / Linux：`source .venv/bin/activate`
- Windows：`.venv\Scripts\activate`（建的时候用 `py -3.12 -m venv .venv`）

装依赖：

```bash
pip install -r requirements.lock.txt
```

这份是锁死版本、带校验的，跟 Docker 和 Windows 包装的是同一套。装不上的话换成
`pip install -r requirements.txt`。

## 起

```bash
python package/launcher.py
```

一个窗口里同时跑着 Loci 和网关，哪个停了几秒后自己拉起来。Ctrl+C 或者关掉窗口，两个一起停。

第一次起的时候：生成钥匙和网关的口令（`data/settings.env`），写好配置（`data/buckets/config.yaml`），
然后自动打开面板 `http://127.0.0.1:18002/loci`。之后跟 README 第二部分从「打开面板」那一步起一样。

## 东西都在哪

全在仓库里的 `data/` 文件夹（git 不管它）：记忆、配置、钥匙、网关的账。**备份就是拷这个文件夹。**

## 改端口、改时区

在 `data/settings.env` 里加一行，关掉窗口再起：

- `LOCI_PORT=18002`：面板和 MCP 的端口
- `LOCI_GATEWAY_PORT=3100`：网关的端口
- `LOCI_TZ=America/Los_Angeles`：不在东八区的改成自己的

端口被别的程序占着的话，起的时候会直接说是哪个、怎么换。

## 本地向量模型

不要本地模型的，在面板 setting 页「向量模型」填云端的就行。要本地的：

1. 装 [Ollama](https://ollama.com)，然后 `ollama pull bge-m3`
2. 面板 setting 页「向量模型」：地址填 **`http://127.0.0.1:11434/v1`**，模型 `bge-m3`

出厂配置里写的地址是 `http://ollama:11434/v1`，那是 Docker 里的名字，直接装的时候连不上，**一定要改**。

## 开机自动起

- Windows：`python package/launcher.py --autostart on`，不要了就 `--autostart off`
- Mac / Linux：没有这个开关，用系统自己的（macOS 的 launchd、Linux 的 systemd），
  让它在这个文件夹里、用 `.venv` 里的 Python 跑 `package/launcher.py`

## 升级

1. 关掉窗口
2. `git pull`
3. 进虚拟环境，`pip install -r requirements.lock.txt`
4. 这一版要不要迁移记忆库，看 [CHANGELOG](../CHANGELOG.md)。要的话：

   ```bash
   python scripts/migrate.py --buckets data/buckets            # 先看要改什么
   python scripts/migrate.py --buckets data/buckets --apply    # 再改，改之前自动备份
   ```

5. 再起
