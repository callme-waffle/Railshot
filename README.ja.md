<p align="center">
  <a href="https://railshot.io/">
    <img src="docs/assets/readme/railshot-wordmark.svg" alt="RAILSHOT" width="480">
  </a>
</p>

[한국어](README.md) | **日本語**

**One Action, Infinite Clouds**

一度のリクエストで、デプロイから管理・運用まで。

RAILSHOTは、Web画面や普段使っているAIエージェントからプロジェクトを送信すると、デプロイ用ファイルの準備、エラーの修正、コンテナイメージの公開、クラウド・オンプレミス環境への反映、公開URLの確認までを進めるデプロイプラットフォームです。デプロイ後も同じ画面でアプリの状態や実行ログを確認し、更新・停止・再開・削除を行えます。

[サービスを開く](https://railshot.io/) · [Notion発表資料](https://app.notion.com/p/3ed8bee9ada48088ab99d21c268da793) · [設計ドキュメント](docs/architecture/README.md) · [MCP接続ガイド](apps/agent/README.md)

## 使い方

1. **プロジェクトを送信します。** Web画面でフォルダ・ZIP・公開GitHubリポジトリのURLを指定するか、MCPを接続したエージェントにデプロイを依頼します。
2. **アプリ名と実行環境を選びます。** 登録済みのAWS・GCP環境、または接続済みのOpenStackオンプレミス環境を使用します。選択できる環境は、各環境の準備状況に応じて表示されます。
3. **進捗を確認します。** 基本チェックに失敗した場合は、AIが許可された範囲で修正案を作成します。同じチェックに合格すると、次の段階へ進みます。
4. **検証済みのURLからアプリを開きます。** イメージの公開後、対象クラスタへの反映と公開HTTPエンドポイントの検証を経て、接続先URLを返します。
5. **デプロイ後も管理を続けます。** デプロイ履歴、AIが変更したファイル、チェック結果、環境のメトリクスやアプリのログを確認し、更新・停止・再開を行えます。

Webからのデプロイは[railshot.io](https://railshot.io/)で始められます。自身のOpenStack環境を接続する場合は、[利用者向けインストーラ](docs/architecture/client-bootstrap.md)と[個人環境管理のAPI仕様](docs/api/personal-environments.md)を参照してください。

### 実際のデプロイ・管理画面

**プロジェクトの送信と環境選択。** フォルダ・ZIP・公開GitHub URLを指定し、クラウドまたはオンプレミスの実行環境を選択します。

[![プロジェクトの入力と実行環境を選ぶWeb画面](docs/assets/readme/new-deployment.png)](docs/assets/readme/new-deployment.png)

<sub>発表資料の画面と図を使用しています。図中の説明は韓国語です。画像をクリックすると原寸で表示できます。[画像の出典](docs/assets/readme/README.md)</sub>

## AIが修正し、チェック結果で判定

基本チェックに失敗すると、許可された範囲でAIが修正し、同じチェックを再実行します。合格したイメージだけを公開し、範囲外の変更や試行上限に達した処理は停止します。

[![AIによる失敗分析・修正・再チェックの流れ](docs/assets/readme/agent-pipeline.png)](docs/assets/readme/agent-pipeline.png)

デプロイの成否は、AIによる完了報告だけでは判断しません。修正が許可範囲に収まっていることと実際のチェック結果を確認し、合格したイメージのダイジェストをそのままデプロイに使用します。

| 段階 | 処理内容 | 次の段階に進む条件 |
| --- | --- | --- |
| 基本チェック | 変更範囲・機密情報の扱い（L0）、デプロイ設定（L1）、イメージビルド（L2）、コンテナ起動・HTTP（L3）を確認します。 | 必須チェックすべてに合格すること。 |
| デプロイ用ファイルの準備 | 必要なDockerfileやデプロイ定義をルールに沿って準備し、必要に応じてAIが補完します。 | パッケージング段階で許可された変更範囲内で、提案が検証されること。 |
| エラーの修正 | 実際に発生した失敗と関連ログをもとに、AIが修正案を作成します。 | ビルド・実行の失敗が確認された場合に限り、ポリシーで許可されたソース変更が可能です。テストやチェック自体は変更しません。 |
| 再チェック | 変更を適用した後、L0から必須チェックを再実行します。 | 設定された試行回数以内にチェックに合格すること。 |
| イメージの公開 | 検証済みイメージを、出所とハッシュの情報とともにGHCRへ公開します。 | 公開段階ではソースを再ビルドせず、検証済みの成果物を確認すること。 |
| デプロイ・接続確認 | GitOpsの宣言を反映し、Argo CD、実際のワークロード、公開HTTPエンドポイントを確認します。 | 検証済みのデプロイ結果と接続先URLを返せること。 |

AIには失敗の要点と関連ログをサイズ制限付きで渡し、追加の根拠が必要な場合に詳しい情報を参照させます。パッケージングとエラー修正の試行回数は別々に管理し、実行ごとの設定と実際の呼び出し回数を記録します。最初のチェックに合格する入力では、AIの呼び出しは不要です。詳しくは[CIパイプライン](ci/README.md)、[デプロイ準備の指針](ci/scripts/agents/skills/prepare-deployment/SKILL.md)、[修正に使用する根拠情報の構成](ci/scripts/runner/repair_evidence.py)を参照してください。

## アーキテクチャ

運用API、CI実行環境、利用者のアプリを実行するクラスタを分離しています。Web画面とMCPは同じ製品APIを使用し、各クラウド・オンプレミスの接続機構が、登録された環境に合わせてデプロイを実行します。

[![運用クラスタと利用者のアプリクラスタを分離したサービス構成](docs/assets/readme/service-serving.png)](docs/assets/readme/service-serving.png)

AWSでのサービス提供構成を示しています。運用クラスタのAPI・ビルド・Argo CDと、利用者のアプリを実行するクラスタを分離し、公開アクセスとデプロイ制御の経路を区別します。

イメージの公開（`published`）、クラスタへの反映、公開URLの検証は、それぞれ別の結果です。MCPがデプロイ要求を受け付けたり、AIの修正が成功したりしても、その時点でアプリのデプロイが完了したとは限りません。直近のデプロイ試行と、最後に検証されたサービス状態も区別して表示します。収集できていないメトリクスを正常値で埋めることはありません。

[全体設計](docs/architecture/README.md) · [アプリ・環境・実行の管理](docs/architecture/application-management.md) · [CIからCDへの受け渡し仕様](docs/api/ci-publication.md) · [デプロイ方式と検証基準](docs/architecture/application-deployment-strategies.md)

## 普段使っているエージェントから利用する

リモートMCPの接続先は `https://railshot.io/mcp` です。OAuthで接続します。Codex CLIでは、次のコマンドで登録できます。

```sh
codex mcp add railshot --url https://railshot.io/mcp
codex mcp login railshot --oauth-client-registration dcr
```

RAILSHOTのWeb画面を利用していたブラウザで接続を承認すると、そのWebセッションに接続されます。例えば「この公開GitHubリポジトリを、AWSに `my-app` という名前でデプロイして」と依頼できます。

| ツール | 用途 |
| --- | --- |
| `list_options`, `list_targets` | 利用可能な環境と登録済みのデプロイ先を確認します。 |
| `deploy_repository` | 公開GitHubリポジトリのデプロイを要求します。 |
| `get_deployment_progress`, `get_deployment` | 進行段階、AIの作業内容、最終的なデプロイ結果を確認します。 |
| `get_app_overview`, `get_deployment_evidence` | デプロイ・運用の概要と、ビルド・デプロイ・実行の追加情報を取得します。 |

リモートMCPは公開GitHubリポジトリを入力として受け付けます。**ローカルのフォルダやZIPをエージェントからデプロイする場合**は、利用者のコンピュータ上で動作する別の `railshot-local` MCPと `deploy_local_project` を使用します。ローカルMCPのセッションは、リモートOAuthのセッションと自動では共有されません。セットアップ手順とChatGPT・Claudeの接続方法は[MCPドキュメント](apps/agent/README.md)を参照してください。

## ローカル開発

**Node.js 22.13以降とnpm**が必要です。リポジトリのルートで依存パッケージをインストールし、APIとダッシュボードを別々のターミナルで起動します。

```sh
npm ci --ignore-scripts

# ターミナル1: API http://127.0.0.1:4173
npm start --workspace railshot-api

# ターミナル2: ダッシュボード http://127.0.0.1:4181
npm run dev
```

ダッシュボードの開発サーバーは `/api/` へのリクエストをローカルAPIへ転送します。上記のコマンドで起動するのはローカル開発サーバーです。実際のデプロイには、GitHub、CIワーカー、デプロイ先、CDに関する運用者側の設定が必要です。未設定の状態で `/healthz` が応答しても、クラウドへのデプロイ準備が整ったことを意味しません。

```sh
# APIとMCPのチェック: 実際のモデルやクラウドは呼び出しません
npm test --workspace railshot-api
npm test --workspace @railshot/agent

# ダッシュボードの本番用ビルド
npm run build
```

Python・Terraform・Ansible・ランタイムのチェックについては[CIガイド](ci/README.md)を参照してください。サービスの環境変数は、[API設定](apps/api/README.md)、[MCP設定](apps/agent/README.md)、[プラットフォームのコンテナデプロイ](docs/architecture/container-deployment.md)に記載しています。

## リポジトリ構成

| パス | 役割 |
| --- | --- |
| [`apps/`](apps/README.md) | Webダッシュボード、製品API・CLI、MCPサーバーとエージェント |
| [`ci/`](ci/README.md) | アプリのチェック、AI修正、検証済みイメージの公開、プラットフォームのPRチェック |
| [`infrastructure/`](infrastructure/ansible/README.md) | クラウドプロバイダーとの接続、Terraform・Ansibleによるインフラ準備 |
| [`deployment/`](deployment/README.md) | Kubernetesランタイムとプラットフォームのデプロイ |
| [`gitops/`](gitops/README.md) | デプロイ宣言、Argo CD連携、反映結果の確認 |
| [`observability/`](observability/README.md) | 環境のメトリクス、ログ、HTTPの観測 |
| [`docs/`](docs/architecture/README.md) | アーキテクチャ、API仕様、意思決定、各時点の検証記録 |

リポジトリの作業指針は[AGENTS.md](AGENTS.md)に従います。変更はPRでレビューし、統合手順は[Gitflowドキュメント](docs/integration/gitflow.md)に従います。過去のPoC・統合記録にある対応範囲や未完了項目は、記録当時の状況です。

リンク先の詳細ドキュメントは主に韓国語です。
