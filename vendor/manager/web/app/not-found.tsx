'use client';
import {motion} from 'motion/react';
import {Button} from '@/components/ui/button';
import {Home} from 'lucide-react';
import Link from 'next/link';

export default function NotFound() {
  return (
    <div className="fixed inset-0 flex items-center justify-center dark:bg-black bg-white">
      <div className="mx-auto max-w-7xl px-4 text-center sm:px-6 md:px-8 lg:px-12">
        <motion.div
          initial={{opacity: 0, y: 20}}
          animate={{opacity: 1, y: 0}}
          transition={{duration: 0.6, delay: 0.2}}
        >
          <p className="font-bold text-xl md:text-4xl dark:text-white text-black">
            {'页面'.split('').map((word, idx) => (
              <motion.span
                key={idx}
                className="inline-block"
                initial={{x: -10, opacity: 0}}
                animate={{x: 0, opacity: 1}}
                transition={{duration: 0.5, delay: idx * 0.04}}
              >
                {word}
              </motion.span>
            ))}
            <span className="text-neutral-400">
              {'未找到'.split('').map((word, idx) => (
                <motion.span
                  key={idx}
                  className="inline-block"
                  initial={{x: -10, opacity: 0}}
                  animate={{x: 0, opacity: 1}}
                  transition={{duration: 0.5, delay: (idx + 2) * 0.04}}
                >
                  {word}
                </motion.span>
              ))}
            </span>
          </p>
        </motion.div>

        <motion.div
          initial={{opacity: 0, y: 20}}
          animate={{opacity: 1, y: 0}}
          transition={{duration: 0.6, delay: 0.6}}
        >
          <p className="text-sm md:text-lg text-neutral-500 mx-auto max-w-2xl py-4">
            抱歉，您访问的页面不存在或已被永久移动。
          </p>
        </motion.div>

        <motion.div
          initial={{opacity: 0, y: 20}}
          animate={{opacity: 1, y: 0}}
          transition={{duration: 0.6, delay: 1.0}}
          className="flex justify-center pt-6"
        >
          <Link href="/dashboard">
            <Button size="lg" className="rounded-full">
              <Home className="mr-2 h-4 w-4" />
              返回首页
            </Button>
          </Link>
        </motion.div>
      </div>
    </div>
  );
}
