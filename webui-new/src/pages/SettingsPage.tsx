import { PageHeader } from '@/components/layout/PageHeader'
import { ApiKeyManager } from '@/components/settings/ApiKeyManager'
import { AdminPasswordManager } from '@/components/settings/AdminPasswordManager'
import { EnvPreview } from '@/components/settings/EnvPreview'
import { RuntimeConfigManager } from '@/components/settings/RuntimeConfigManager'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

export default function SettingsPage() {
  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="设置"
        description="配置模型、API Key 与管理员密码，并查看当前运行时配置"
      />
      <Tabs defaultValue="model" className="mx-auto w-full max-w-[1180px] space-y-4">
        <TabsList className="grid h-auto w-full grid-cols-2 gap-1 p-1 sm:grid-cols-4">
          <TabsTrigger value="model" className="h-8">模型与计费</TabsTrigger>
          <TabsTrigger value="access" className="h-8">访问密钥</TabsTrigger>
          <TabsTrigger value="security" className="h-8">安全</TabsTrigger>
          <TabsTrigger value="diagnostics" className="h-8">诊断</TabsTrigger>
        </TabsList>
        <TabsContent value="model">
          <RuntimeConfigManager />
        </TabsContent>
        <TabsContent value="access">
          <ApiKeyManager />
        </TabsContent>
        <TabsContent value="security">
          <AdminPasswordManager />
        </TabsContent>
        <TabsContent value="diagnostics">
          <EnvPreview />
        </TabsContent>
      </Tabs>
    </div>
  )
}
