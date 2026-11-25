package main

// OllamaRunners: the runner processes of the Ollama this agent supervises, as a fact (agent 0.37.0, ADR-0148).
//
// Ollama starts one runner per loaded model (ollama runner, llama-server) as its own child, and the runner holds the
// model's main memory. After keep_alive 0 the model leaves /api/ps at once, but the runner keeps its memory for seconds
// until it has exited. A tool that unloads and then starts a large job (a VM) pushed Windows to 93 % RAM in that gap;
// neither /api/ps nor the node's available RAM says when the memory is back. Count 0 says it: no runner process lives,
// so none holds memory.
type OllamaRunners struct {
	// Count: processes in Ollama's process tree besides the server itself and the console host Windows starts for it.
	Count int `json:"count"`
	// PrivateMiB: their private working sets, summed (procMemory, ADR-0092): what they hold in RAM that only paging out
	// would free. File pages of the model they read go to the standby list when they exit and are not counted.
	PrivateMiB int `json:"private_mib"`
}

// ollamaRunners: the fact for the node, summed over every running Ollama child (usually one; one per GPU is possible).
// false = the agent cannot tell: Ollama is not its child or not running (held for an update, restarting), or a child's
// process tree cannot be listed in full (outside Windows it is never listed).
func (s *Supervisor) ollamaRunners() (OllamaRunners, bool) {
	if s == nil {
		return OllamaRunners{}, false
	}
	type server struct {
		tree *procTree
		root uint32
	}
	var servers []server
	s.mu.Lock()
	for _, c := range s.children {
		if c.Running && c.tree != nil && isOllamaExe(c.spec.Cmd) {
			servers = append(servers, server{c.tree, uint32(c.PID)})
		}
	}
	s.mu.Unlock()
	if len(servers) == 0 {
		return OllamaRunners{}, false
	}
	var sum OllamaRunners
	for _, sv := range servers {
		pids, listed := sv.tree.members()
		if !listed {
			return OllamaRunners{}, false // one tree unknown: no count for the node, never a partial one
		}
		r := countRunners(pids, sv.root, isConsoleHost, procMemory)
		sum.Count += r.Count
		sum.PrivateMiB += r.PrivateMiB
	}
	return sum, true
}

// countRunners: every process of the tree except the server (root) and a console host. It errs on the side of "still
// held": a process whose program cannot be read counts as a runner, and one whose memory cannot be read counts with
// 0 MiB, so the count never reaches 0 while a process of the tree other than those two still lives.
func countRunners(pids []uint32, root uint32, consoleHost func(uint32) bool, memory func(uint32) (procMem, bool)) OllamaRunners {
	var r OllamaRunners
	var private uint64
	for _, pid := range pids {
		if pid == root || consoleHost(pid) {
			continue
		}
		r.Count++
		if m, ok := memory(pid); ok {
			private += m.Private
		}
	}
	r.PrivateMiB = int(private >> 20)
	return r
}
