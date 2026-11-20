# Tutorial 5: LPMによる宛先IPv4アドレスの転送

Tutorial 4では、宛先IPv4アドレスの完全一致（exact match）によって出力ポートを決定した。本チュートリアルでは、同じ宛先IPv4アドレスを検索キーとし、最長プレフィックス一致（Longest Prefix Match：LPM）による転送を実行する。

## 目的

IPv4アドレスのプレフィックス長に応じたテーブル検索を行い、次の3点を確認する。

1. `/30`のエントリが複数の宛先IPv4アドレスに一致すること。
2. 複数のエントリが一致した場合、プレフィックス長が最も長いエントリが選択されること。
3. テーブルの一致判定と、指定された出力ポートへの転送は異なる処理であること。

P4Runtime Shellによるエントリの登録方法、Mininetの基本操作、およびP4プログラムのコンパイル方法の詳細は、Tutorial 3・4と[雑多なこと：P4プログラムのコンパイル](tm_misc.md)を参照する。

## LPMの一致条件

IPv4アドレスは32ビットで構成される。LPMでは、登録されたプレフィックス長だけ宛先IPv4アドレスの先頭ビットを比較する。例えば`10.0.0.4/30`は先頭30ビットを比較し、末尾2ビットを比較しない。

| 項目 | 値 |
| --- | --- |
| 登録するネットワークアドレス | `10.0.0.4/30` |
| サブネットマスク | `255.255.255.252` |
| 比較対象 | 先頭30ビット |
| 比較対象外（ワイルドカード） | 末尾2ビット |
| 一致するIPv4アドレス | `10.0.0.4`～`10.0.0.7` |

末尾2ビットの値は`00`、`01`、`10`、`11`の4通りを取り得る。このため、宛先IPv4アドレスが`10.0.0.5`または`10.0.0.6`のパケットも、同じ`10.0.0.4/30`に一致する。LPMで比較対象外となるのは末尾の連続したビットであり、任意の位置のビットを個別に無視する方式ではない。

`10.0.0.5/30`と表記した場合も所属するアドレス範囲は同じだが、転送テーブルにはホスト部を0にしたネットワークアドレス`10.0.0.4/30`を登録する。

## 実験環境

Mininet上にP4スイッチ`s1`を1台、ホスト`h1`～`h12`を12台配置する。`single,12`トポロジーでは、ホスト`hN`のIPv4アドレスは`10.0.0.N`となり、スイッチ`s1`のポート`N`に接続される。

| ホスト | IPv4アドレス | s1の接続ポート | 本実験での役割 |
| --- | --- | --- | --- |
| h1 | `10.0.0.1` | 1 | ping送信元・応答の転送先 |
| h5 | `10.0.0.5` | 5 | `/30`と`/32`の優先関係を確認 |
| h6 | `10.0.0.6` | 6 | `/30`で指定する転送先 |
| h9 | `10.0.0.9` | 9 | 未登録アドレスの確認 |

他のホストも同じ規則で接続される。ホスト側のIPアドレス設定と、P4スイッチが参照するLPMエントリは独立している。本実験の`/30`はP4の転送テーブルの一致条件であり、各ホストに`/30`のサブネットマスクを設定することを意味しない。

## P4プログラムの変更点

本実験には`proto05/proto05.p4`を使用する。Tutorial 4の`proto04.p4`から変更するのは、`ipv4_table`のマッチタイプである。

```p4
table ipv4_table {
    key = {
        hdr.ipv4.dstAddr : lpm;
    }
    actions = {
        forward;
        drop;
    }
    size = 1024;
    default_action = drop();
}
```

Tutorial 4の`exact`を`lpm`に変更している。`forward`、`drop`、IPv4ヘッダの解析、その他の処理は変更していない。LPMの適用にはP4プログラムの変更と再コンパイルが必要であり、既存の`proto04.json`と`p4info.txtpb`をそのまま使用することはできない。

## 実験手順

### STEP 01 P4プログラムをコンパイルする

リポジトリの`proto05`ディレクトリを、Tutorial 0で作成した`/tmp/P4Runtime-protoswitch`に配置する。リポジトリのルートで次を実行する。

```bash
$ cp -r proto05 /tmp/P4Runtime-protoswitch/
```

続いてMacのターミナルで次を実行し、BMv2向けのJSONとP4Infoを生成する。

```bash
$ docker run --rm \
  -v /tmp/P4Runtime-protoswitch:/tmp \
  -w /tmp/proto05 \
  p4lang/p4c \
  p4c --target bmv2 --arch v1model \
  --p4runtime-files p4info.txtpb proto05.p4
```

コンパイル後、`proto05`内に`proto05.json`と`p4info.txtpb`が生成される。Dockerイメージのアーキテクチャに関する注意点は、Tutorial 0および[コンパイル方法](tm_misc.md)を参照する。

### STEP 02 MininetとP4Runtime Shellを起動する

最初にMininetを起動する。`--arp`によって各ホストにARPエントリを事前登録し、IPv4パケットの転送を観察する。

```bash
$ docker run --privileged --rm -it \
  -p 50001:50001 \
  -e LOGLEVEL=debug -e PKTDUMP=true -e IPV6=false \
  yutakayasuda/p4mn --topo single,12 --mac --arp
```

`mininet>`で`net`を実行し、`h1`が`s1-eth1`、`h5`が`s1-eth5`、`h6`が`s1-eth6`に接続されていることを確認する。

別のターミナルからP4Runtime Shellを起動する。

```bash
$ docker run --rm -ti \
  -v /tmp/P4Runtime-protoswitch:/tmp \
  p4lang/p4runtime-sh \
  --grpc-addr host.docker.internal:50001 \
  --device-id 1 --election-id 0,1 \
  --config /tmp/proto05/p4info.txtpb,/tmp/proto05/proto05.json
```

P4Runtime Shellでテーブル定義を表示する。

```python
P4Runtime sh >>> tables["MyIngress.ipv4_table"]
```

検索フィールドが`hdr.ipv4.dstAddr`、マッチタイプが`LPM`であることを確認する。

### STEP 03 /30の転送エントリを登録する

pingの要求と応答を転送するため、宛先IPv4アドレス`10.0.0.0/30`をポート1へ、`10.0.0.4/30`をポート6へ転送する。

```python
P4Runtime sh >>> te = table_entry["MyIngress.ipv4_table"](action="MyIngress.forward")
P4Runtime sh >>> te.match["hdr.ipv4.dstAddr"] = "10.0.0.0/30"
P4Runtime sh >>> te.action.set(port="1")
P4Runtime sh >>> te.insert()

P4Runtime sh >>> te = table_entry["MyIngress.ipv4_table"](action="MyIngress.forward")
P4Runtime sh >>> te.match["hdr.ipv4.dstAddr"] = "10.0.0.4/30"
P4Runtime sh >>> te.action.set(port="6")
P4Runtime sh >>> te.insert()
```

登録したエントリを読み出す。

```python
P4Runtime sh >>> table_entry["MyIngress.ipv4_table"].read(lambda e: print(e))
```

`10.0.0.0/30`と`10.0.0.4/30`について、LPMフィールドの`prefix_len`がいずれも`30`となり、対応するアクションのポート番号がそれぞれ`1`、`6`であることを確認する。

### STEP 04 /30による転送を検証する

Mininetから、h1を送信元としてh6へpingを送信する。

```bash
mininet> h1 ping -c 3 h6
```

要求パケットの宛先`10.0.0.6`は`10.0.0.4/30`に一致し、ポート6へ転送される。応答パケットの宛先`10.0.0.1`は`10.0.0.0/30`に一致し、ポート1へ転送される。両方向の転送が成立するため、正常な状態ではpingの応答を受信できる。

次に、送信先をh5へ変更する。

```bash
mininet> h1 ping -c 3 h5
```

宛先`10.0.0.5`も`10.0.0.4/30`に一致するため、パケットはポート6へ転送される。しかしh5が接続されているのはポート5である。そのため、この時点でh5からの応答は得られない。

この失敗は**テーブルに一致するエントリが存在しないためではない**。LPMによる一致判定は成立しているが、アクションに指定した出力ポートが宛先ホストの接続ポートと異なるために生じる。

必要に応じて、Mininet上で以下を実行すると、ポート6から送信されたパケットを記録したPCAPを確認できる。

```bash
mininet> sh tcpdump -nn -r /tmp/s1-eth6_out.pcap
```

表示されるICMPパケットの宛先が`10.0.0.5`であれば、h5宛てパケットがポート6から出力されたことを確認できる。

### STEP 05 /32のエントリを追加する

`10.0.0.5`宛てのパケットのみをポート5へ転送するエントリを追加する。

```python
P4Runtime sh >>> te = table_entry["MyIngress.ipv4_table"](action="MyIngress.forward")
P4Runtime sh >>> te.match["hdr.ipv4.dstAddr"] = "10.0.0.5/32"
P4Runtime sh >>> te.action.set(port="5")
P4Runtime sh >>> te.insert()
```

この時点で、宛先`10.0.0.5`には`10.0.0.4/30`と`10.0.0.5/32`の2件が一致する。LPMではより長い`/32`のエントリが優先されるため、ポート5が選択される。一方、宛先`10.0.0.6`には`/32`のエントリが存在しないため、従来どおり`/30`によってポート6が選択される。

Mininetから、次の2つの通信を実行する。

```bash
mininet> h1 ping -c 3 h5
mininet> h1 ping -c 3 h6
```

h5とh6の両方から応答が得られれば、プレフィックス長によるエントリの選択を確認できる。エントリの登録順序ではなく、プレフィックス長の大小によって選択される点に注意する。

### STEP 06 未登録アドレスへの通信を確認する

最後に、転送エントリのないh9宛ての通信を実行する。

```bash
mininet> h1 ping -c 3 h9
```

宛先`10.0.0.9`は登録したいずれのプレフィックスにも一致しない。そのため、`ipv4_table`の`default_action = drop()`が適用される。この場合は、STEP 04のh5宛て通信とは異なり、**一致するエントリが存在しないことによる破棄**である。

## 実験結果の整理

| 操作 | 宛先IPv4アドレス | 選択されるエントリ | 出力ポート | 期待されるping結果 |
| --- | --- | --- | --- | --- |
| `/30`のみ登録 | `10.0.0.6`（h6） | `10.0.0.4/30` | 6 | 成功 |
| `/30`のみ登録 | `10.0.0.5`（h5） | `10.0.0.4/30` | 6 | 失敗（h5はport5） |
| `/32`追加後 | `10.0.0.5`（h5） | `10.0.0.5/32` | 5 | 成功 |
| `/32`追加後 | `10.0.0.6`（h6） | `10.0.0.4/30` | 6 | 成功 |
| 全エントリ登録後 | `10.0.0.9`（h9） | 一致なし | 破棄 | 失敗 |

本実験のように各ホストがスイッチの異なるポートに直接接続されている場合、単一の`/30`エントリによって4台すべてのホストへ正しく転送することはできない。通常のIPルーティングでは、同じネットワークプレフィックスに属する複数の宛先に対し、そのネットワークへ到達する共通の出力インターフェースまたは次ホップを指定する。本実験ではLPMの一致判定と最長一致の優先関係を観察するため、意図的に1つのプレフィックスに複数のホストが含まれる構成を使用している。

上記の通信結果は設定に基づく期待結果であり、実測値ではない。実際の実行では、テーブルの読み出し結果、pingの送受信数、および必要に応じてPCAPを照合する。
