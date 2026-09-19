# flop-watch

`flop-labs` と `flop.finance` の変更を1時間ごとに検知して、このリポジトリに Issue を立てる。
GitHub Actions のみで動く。サーバーも常駐も不要。

## 何を見ているか

| 対象 | 検知するもの |
|---|---|
| `flop-labs` のリポジトリ一覧 | **新しいリポジトリ**（テストネットやフォーセットは、記事になる前にここに現れる） |
| 各リポジトリのリリース | 新しいタグ |
| `flop-labs/yellowpaper`・`technocore-chat` | 新しいコミット |
| `flop-labs/*` の Issue | **運営が立てた Issue と、運営が付けたコメント** |
| `flop.finance` 各ページ・`technocore.chat/llms.txt` | 本文の変更（**実際の差分を Issue に出す**） |

### 運営の判定

GitHub API はコメントごとに投稿者の立場を `author_association` で返す。
`OWNER` / `MEMBER` / `COLLABORATOR` に絞ることで、参加者の投稿を除いて
運営の発言だけを拾う。`config.json` の `operator_roles` で変えられる。

これを入れた理由: 仕様の穴や未公開の判定基準が最初に明かされるのは Issue のコメントで、
公式サイトでも報道でもない。全部を追うのは流量的に不可能なので、運営の発言だけに絞る。

### ページの差分

HTML からタグ・スクリプト・コメントを落として本文だけを抽出し、`snapshots/` に保存する。
次回はそれと比較して unified diff を Issue に貼る。
ナンスや装飾の揺れでは発火しない（抽出後のテキストが同一なら差分ゼロ）。

## 使い方

1. このリポジトリを作る（public でも private でもよい）
2. Actions タブ → **flop watch** → **Run workflow** → mode に `seed` を選んで実行
   （初回。現状を記録するだけで Issue は立てない）
3. 以降は毎時07分に自動実行される

手元で試すなら:

```
python3 watch.py --seed      # 現状を記録
python3 watch.py --dry-run   # Issue を立てず内容を表示
```

`GITHUB_TOKEN` なしでも動くが、未認証は 60回/時 の制限に当たる。
Actions 上では自動で渡されるので 1000回/時 になる。

## 通知

検知すると Issue が1本立つ。GitHub の通知設定でメールが飛ぶ。
自分のリポジトリなので既定で Watch されている。

複数の変化はまとめて1本の Issue になる。同じ変化を二度通知することはない
（`state.json` に前回の状態を記録し、Actions が毎回コミットして戻す）。

## API 消費

1回の実行で約12回。毎時実行で1日 288回。認証済みの上限 1000回/時 に対して十分余裕がある。

## 注意

- `state.json` と `snapshots/` は Actions が自動でコミットする。手で編集しない
- ページの取得に失敗しても、その回はスキップして次に進む（サイトの一時的な不調で止まらない）
- 監視対象を足すなら `config.json` の `pages` / `commit_watch` に URL を追加する
