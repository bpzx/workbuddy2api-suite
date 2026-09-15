'use client';

import {useEffect, useState} from 'react';
import {useRouter} from 'next/navigation';
import {motion} from 'motion/react';
import {Bot, Loader2, LogIn} from 'lucide-react';
import {errText} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';

export default function LoginPage() {
  const {me, loading, login} = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!loading && me) router.replace('/dashboard');
  }, [loading, me, router]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError('');
    setBusy(true);
    try {
      await login(username.trim(), password);
      router.replace('/dashboard');
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bg-background flex min-h-svh flex-col items-center justify-center gap-6 p-6 md:p-10">
      <motion.div
        initial={{opacity: 0, y: 12}}
        animate={{opacity: 1, y: 0}}
        transition={{duration: 0.35}}
        className="w-full max-w-sm"
      >
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid h-11 w-11 place-items-center rounded-2xl bg-primary text-primary-foreground">
            <Bot className="h-5 w-5" />
          </div>
          <div className="space-y-1">
            <h1 className="text-lg font-semibold tracking-[-0.01em]">WorkBuddy Manager</h1>
            <p className="text-xs text-muted-foreground">
              腾讯 CodeBuddy 账号池管理 · OpenAI 兼容反代网关
            </p>
          </div>
        </div>

        <form onSubmit={submit} className="rounded-[24px] bg-muted p-5">
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="username" className="text-[11px] text-muted-foreground">
                用户名
              </Label>
              <Input
                id="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="admin"
                className="bg-background"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password" className="text-[11px] text-muted-foreground">
                密码
              </Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="请输入密码"
                className="bg-background"
              />
            </div>

            {error && <p className="text-xs text-red-500">{error}</p>}

            <Button type="submit" disabled={busy} className="w-full rounded-full">
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
              登录
            </Button>
          </div>
        </form>

        <p className="mt-6 text-center text-[11px] text-muted-foreground">
          登录即表示您已获得该系统的访问授权，所有操作将被记录。
        </p>
      </motion.div>
    </div>
  );
}
