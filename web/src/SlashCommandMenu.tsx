import { useEffect, useRef } from "react";

export interface SlashCommand {
  id: string;
  name: string;
  label: string;
  description: string;
  icon: string;
  category: "Agent Mode" | "Automation" | "Actions";
  actionType: "insert" | "execute";
  insertText?: string;
}

export const SLASH_COMMANDS: SlashCommand[] = [
  {
    id: "goal",
    name: "/goal",
    label: "Autonomous Goal",
    description: "Run a thorough, multi-step task until the goal is fully achieved",
    icon: "🎯",
    category: "Agent Mode",
    actionType: "insert",
    insertText: "/goal ",
  },
  {
    id: "plan",
    name: "/plan",
    label: "Step-by-Step Plan",
    description: "Generate a structured engineering plan before executing code changes",
    icon: "📋",
    category: "Agent Mode",
    actionType: "insert",
    insertText: "/plan ",
  },
  {
    id: "grill-me",
    name: "/grill-me",
    label: "Design Interview",
    description: "Interactive interview to resolve tradeoffs and clarify ambiguities",
    icon: "🔥",
    category: "Agent Mode",
    actionType: "insert",
    insertText: "/grill-me ",
  },
  {
    id: "teamwork-preview",
    name: "/teamwork-preview",
    label: "Teamwork Subagents",
    description: "Orchestrate autonomous subagents collaborating on tasks",
    icon: "👥",
    category: "Agent Mode",
    actionType: "insert",
    insertText: "/teamwork-preview ",
  },
  {
    id: "schedule",
    name: "/schedule",
    label: "Schedule Task",
    description: "Run an instruction on a recurring cron schedule or one-shot timer",
    icon: "⏱",
    category: "Automation",
    actionType: "insert",
    insertText: "/schedule ",
  },
  {
    id: "learn",
    name: "/learn",
    label: "Learn Skill",
    description: "Propose a durable, governed reusable skill from a workflow or procedure",
    icon: "🎓",
    category: "Agent Mode",
    actionType: "insert",
    insertText: "/learn ",
  },
  {
    id: "remember",
    name: "/remember",
    label: "Remember Fact / Rule",
    description: "Directly save a durable preference, project decision, or rule to memory",
    icon: "✦",
    category: "Agent Mode",
    actionType: "insert",
    insertText: "/remember ",
  },
  {
    id: "compact",
    name: "/compact",
    label: "Compact Context",
    description: "Summarize and compact older chat history to free context window tokens",
    icon: "🗜️",
    category: "Actions",
    actionType: "execute",
  },
  {
    id: "search",
    name: "/search",
    label: "Search Chats",
    description: "Open the full-text search across all conversations and messages",
    icon: "🔍",
    category: "Actions",
    actionType: "execute",
  },
  {
    id: "inspect",
    name: "/inspect",
    label: "Toggle Inspector",
    description: "Open or close the slide-over execution run inspector drawer",
    icon: "◎",
    category: "Actions",
    actionType: "execute",
  },
  {
    id: "new",
    name: "/new",
    label: "New Chat",
    description: "Start a fresh, clean conversation session",
    icon: "＋",
    category: "Actions",
    actionType: "execute",
  },
  {
    id: "archive",
    name: "/archive",
    label: "Archive Chat",
    description: "Archive the current active conversation session",
    icon: "📦",
    category: "Actions",
    actionType: "execute",
  },
  {
    id: "clear",
    name: "/clear",
    label: "Clear Draft",
    description: "Clear the current message text in the composer",
    icon: "✕",
    category: "Actions",
    actionType: "execute",
  },
];

interface SlashCommandMenuProps {
  filter: string;
  selectedIndex: number;
  onSelect: (command: SlashCommand) => void;
  onClose: () => void;
}

export function SlashCommandMenu({
  filter,
  selectedIndex,
  onSelect,
  onClose,
}: SlashCommandMenuProps) {
  const listRef = useRef<HTMLDivElement>(null);
  const normalizedFilter = filter.toLowerCase().replace(/^\//, "").trim();

  const filteredCommands = SLASH_COMMANDS.filter((cmd) => {
    if (!normalizedFilter) return true;
    const nameMatch = cmd.name.toLowerCase().includes(normalizedFilter);
    const labelMatch = cmd.label.toLowerCase().includes(normalizedFilter);
    const descMatch = cmd.description.toLowerCase().includes(normalizedFilter);
    return nameMatch || labelMatch || descMatch;
  });

  // Ensure active item is scrolled into view smoothly
  useEffect(() => {
    if (!listRef.current) return;
    const activeEl = listRef.current.querySelector(".slash-item.active") as HTMLElement | null;
    if (activeEl) {
      activeEl.scrollIntoView({ block: "nearest" });
    }
  }, [selectedIndex]);

  if (filteredCommands.length === 0) {
    return (
      <div className="slash-menu">
        <div className="slash-menu-header">
          <span className="slash-menu-title">Commands & Functions</span>
          <button className="slash-menu-close" onClick={onClose}>✕</button>
        </div>
        <div className="slash-menu-empty">
          No commands matching &quot;/{normalizedFilter}&quot;
        </div>
      </div>
    );
  }

  return (
    <div className="slash-menu" role="listbox" aria-label="Slash commands">
      <div className="slash-menu-header">
        <span className="slash-menu-title">
          <span className="slash-glyph">/</span> Commands & Functions
        </span>
        <span className="slash-menu-hint">↑↓ navigate · ↵ select · esc dismiss</span>
      </div>
      <div className="slash-menu-list" ref={listRef}>
        {filteredCommands.map((cmd, index) => {
          const isActive = index === (selectedIndex % filteredCommands.length + filteredCommands.length) % filteredCommands.length;
          return (
            <button
              key={cmd.id}
              className={`slash-item ${isActive ? "active" : ""}`}
              onClick={() => onSelect(cmd)}
              type="button"
              role="option"
              aria-selected={isActive}
            >
              <span className="slash-item-icon">{cmd.icon}</span>
              <div className="slash-item-content">
                <div className="slash-item-head">
                  <span className="slash-item-name">{cmd.name}</span>
                  <span className="slash-item-label">{cmd.label}</span>
                  <span className="slash-item-badge">{cmd.category}</span>
                </div>
                <span className="slash-item-desc">{cmd.description}</span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
