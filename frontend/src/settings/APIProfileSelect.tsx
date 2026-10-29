import type { APIProfileSummary } from "./generationSettings";

// 只从已保存连接中选择本次 API，展示密钥是否就绪而不读取密钥。
export function APIProfileSelect({ profiles, value, disabled, onChange }: {
  profiles: APIProfileSummary[]; value: string; disabled: boolean; onChange: (id: string) => void;
}) {
  return <label className="api-profile-selection">API 配置
    <select value={value} disabled={disabled} onChange={event => onChange(event.target.value)}>
      {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model}</option>)}
    </select>
  </label>;
}
