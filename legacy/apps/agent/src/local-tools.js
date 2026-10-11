import { createHash } from 'node:crypto';
import { isAbsolute } from 'node:path';
import { z } from 'zod';
import { archiveFromPath } from '../../api/src/client.js';
import { archiveLimits, inspectArchive } from '../../api/src/archive.js';
import { sourceArchive } from '../../api/src/http/source.js';
import { tools } from './tools.js';

// Only mcp-local.js imports this capability. Remote MCP never accepts server file paths.
export const localTools = {
  deploy_local_project: {
    title: '로컬 폴더·ZIP 배포 요청',
    description: '사용자가 지정한 절대 경로의 폴더 또는 ZIP을 Railshot API로 직접 업로드합니다. 사용자 배포 지시가 있을 때만 호출하세요. .git·node_modules 등은 제외하고 비밀 파일·심볼릭 링크·크기 위반은 전송 전에 거부합니다. 소스는 모델에 반환하지 않습니다. 접수는 배포 성공이 아닙니다. 결과가 불확실하면 새 요청 키로 다시 보내지 말고 같은 경로·앱·대상·idempotency_key를 유지하세요. 후속 상태와 관측은 같은 로컬 MCP에서 조회하세요.',
    schema: tools.deploy_repository.schema.omit({ repository_url: true }).extend({
      path: z.string().min(1).max(4096).refine(isAbsolute, '절대 경로를 지정하세요.'),
    }).strict(),
    readOnly: false,
    async run(api, { path, ...input }) {
      const archive = await archiveFromPath(path);
      // Reuse the API's ZIP contract before any bytes leave this computer.
      const files = await inspectArchive(archive.bytes);
      // ZIP inputs can contain excluded files too; never transmit those original bytes.
      const bytes = await sourceArchive(files);
      if (bytes.length > archiveLimits.maxBytes) throw new Error('ZIP 파일은 100 MB 이하이어야 합니다.');
      const upload = { files: files.length, bytes: bytes.length,
        sha256: createHash('sha256').update(bytes).digest('hex') };
      const accepted = await api.deployArchive({ ...input, bytes, name: 'source.zip' });
      return { ...accepted, upload };
    },
  },
};
