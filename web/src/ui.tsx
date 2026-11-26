import type { ReactNode, ButtonHTMLAttributes, FC } from 'react'
import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

const twMerge = extendTailwindMerge({
  override: {
    theme: {},
    classGroups: {
      'font-size': ['text-ui', 'text-ui-sm', 'text-ui-lg'],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export type Tone = 'gray' | 'away' | 'feature' | 'success' | 'error' | 'info' | 'teal' | 'warning'

export interface StatusProps {
  tone: Tone
  children: ReactNode
  className?: string
}

export const Status: FC<StatusProps> = ({ tone, children, className }) => {
  const toneClasses: Record<Tone, string> = {
    gray: 'bg-gray',
    away: 'bg-away',
    feature: 'bg-feature',
    success: 'bg-success',
    error: 'bg-error',
    info: 'bg-info',
    teal: 'bg-teal',
    warning: 'bg-warning',
  }

  return (
    <span className={cn('inline-flex items-center gap-1.5 text-ui text-strong', className)}>
      <span className={cn('size-1.5 rounded-full shrink-0', toneClasses[tone])} />
      <span>{children}</span>
    </span>
  )
}

export interface TagProps {
  children: ReactNode
  className?: string
}

export const Tag: FC<TagProps> = ({ children, className }) => {
  return (
    <span
      className={cn(
        'inline-flex items-center h-5 rounded border border-stroke px-1.5 text-[11px] font-medium text-sub font-mono uppercase tracking-wider',
        className
      )}
    >
      {children}
    </span>
  )
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md' | 'lg' | 'icon'
  children: ReactNode
}

export const Button: FC<ButtonProps> = ({
  variant = 'secondary',
  size = 'md',
  className,
  children,
  ...props
}) => {
  const variantStyles = {
    primary: 'bg-primary text-white hover:bg-primary-dark',
    secondary: 'border border-stroke bg-bg-white text-strong hover:bg-bg-weak',
    ghost: 'text-sub hover:bg-bg-weak hover:text-strong',
    danger: 'border border-stroke bg-bg-white text-error hover:bg-error-lighter',
  }

  const sizeStyles = {
    sm: 'h-9 px-3 text-ui-sm lg:h-7 lg:px-2',
    md: 'h-11 px-4 text-ui lg:h-8 lg:px-3',
    lg: 'h-12 px-5 text-ui-lg lg:h-10 lg:px-4',
    icon: 'size-11 p-2 lg:size-8 lg:p-1.5',
  }

  return (
    <button
      className={cn(
        'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl font-medium transition-colors focus-visible:ring-2 focus-visible:ring-primary/40 disabled:opacity-40 lg:gap-1.5 lg:rounded-md [&_svg]:size-5 lg:[&_svg]:size-4',
        variantStyles[variant],
        sizeStyles[size],
        className
      )}
      {...props}
    >
      {children}
    </button>
  )
}
