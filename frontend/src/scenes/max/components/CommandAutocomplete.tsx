import { IconSparkles } from '@posthog/icons'

import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import { twMerge } from 'tailwind-merge'

export interface Command {
    name: string
    description: string
    value: string
    icon?: React.ReactNode
}

interface CommandAutocompleteProps {
    commands: Command[]
    onSelect: (command: Command) => void
    visible: boolean
    className?: string
    focusedIndex: number
    onFocusedIndexChange: (index: number) => void
}

export interface CommandAutocompleteHandle {
    handleKeyDown: (e: React.KeyboardEvent) => boolean
}

export const CommandAutocomplete = forwardRef<CommandAutocompleteHandle, CommandAutocompleteProps>(
    ({ commands, onSelect, visible, className, focusedIndex, onFocusedIndexChange }, ref) => {
        const containerRef = useRef<HTMLDivElement>(null)

        useImperativeHandle(ref, () => ({
            handleKeyDown: (e: React.KeyboardEvent) => {
                if (!visible || commands.length === 0) {
                    return false
                }

                if (e.key === 'ArrowDown') {
                    e.preventDefault()
                    onFocusedIndexChange(Math.min(focusedIndex + 1, commands.length - 1))
                    return true
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault()
                    onFocusedIndexChange(Math.max(focusedIndex - 1, 0))
                    return true
                } else if (e.key === 'Enter') {
                    e.preventDefault()
                    if (focusedIndex >= 0 && focusedIndex < commands.length) {
                        onSelect(commands[focusedIndex])
                    }
                    return true
                } else if (e.key === 'Escape') {
                    e.preventDefault()
                    return true
                }
                return false
            },
        }))

        // Scroll focused item into view
        useEffect(() => {
            if (visible && focusedIndex >= 0 && containerRef.current) {
                const focusedElement = containerRef.current.querySelector(`[data-command-index="${focusedIndex}"]`)
                if (focusedElement) {
                    focusedElement.scrollIntoView({ block: 'nearest', inline: 'nearest' })
                }
            }
        }, [visible, focusedIndex])

        if (!visible || commands.length === 0) {
            return null
        }

        return (
            <div
                ref={containerRef}
                className={twMerge(
                    'absolute z-20 w-full bg-bg-light border border-border rounded-lg shadow-lg max-h-48 overflow-y-auto',
                    'bottom-full mb-2',
                    'animate-in fade-in-0 slide-in-from-bottom-1 duration-150',
                    'backdrop-blur-sm',
                    className
                )}
            >
                <div className="p-1">
                    <div className="text-xs text-text-3000-muted px-3 py-1 mb-1 font-medium">Commands</div>
                    {commands.map((command, index) => (
                        <div
                            key={command.value}
                            data-command-index={index}
                            className={twMerge(
                                'flex items-center gap-3 px-3 py-2.5 rounded-md cursor-pointer transition-all duration-100',
                                'hover:bg-bg-3000 hover:scale-[1.01]',
                                focusedIndex === index && 'bg-bg-3000 scale-[1.01] shadow-sm'
                            )}
                            onClick={() => onSelect(command)}
                        >
                            <div className="flex items-center justify-center w-7 h-7 rounded-md bg-primary-3000 text-primary-3000-contrast shadow-sm">
                                {command.icon || <IconSparkles className="w-4 h-4" />}
                            </div>
                            <div className="flex flex-col gap-1 min-w-0 flex-1">
                                <div className="font-mono text-sm font-semibold text-text-3000">{command.name}</div>
                                <div className="text-xs text-text-3000-muted truncate">{command.description}</div>
                            </div>
                            <div className="text-xs text-text-3000-muted opacity-60 font-mono bg-bg-3000 px-2 py-1 rounded">
                                ⏎
                            </div>
                        </div>
                    ))}
                </div>
                <div className="border-t border-border p-2 bg-bg-3000/50">
                    <div className="text-xs text-text-3000-muted text-center">
                        ↑↓ to navigate • Enter to select • Esc to close
                    </div>
                </div>
            </div>
        )
    }
)

CommandAutocomplete.displayName = 'CommandAutocomplete'

export const AVAILABLE_MAX_COMMANDS: Command[] = [
    {
        name: '/init',
        description: 'Set up knowledge about your product and/or company',
        value: '/init',
        icon: <IconSparkles className="w-4 h-4" />,
    },
]
