// LOW-layer Design-by-Contract example, modeled on samples/critical-path-scheduler.
//
// Mirrors the shape of that sample Component's internal implementation: a forward pass
// computes each task's earliest finish time, a backward pass computes each task's
// latest finish time, and slack = late_finish - early_finish must never be negative for
// a correctly-computed schedule. slackForTask is an internal, dense/algorithmic
// helper -- not the Component's exported entry point (that is covered end-to-end by
// source/tests/manifest.json's black-box cases) -- whose violation would silently
// produce a wrong-but-plausible schedule rather than an obvious crash, which is exactly
// the case the LOW-layer Design-by-Contract paragraph added to
// go-portable-application/SKILL.md describes.
//
// Build and run with `go run source/main.go`-equivalent tooling; see the companion
// NOTES.md for the exact commands and captured output.
package main

import (
	"fmt"
	"sort"
)

// Internal invariant: for any correctly-computed forward/backward pass over a valid
// DAG, lateFinish[taskID] >= earlyFinish[taskID] always holds. Violating this means the
// forward or backward pass has a bug, not that the input schedule is infeasible
// (infeasible input is rejected earlier, before this helper runs).
func slackForTask(taskID int, earlyFinish, lateFinish map[int]int) int {
	early, earlyOK := earlyFinish[taskID]
	if !earlyOK {
		panic(fmt.Sprintf("slackForTask: unknown taskID %d in earlyFinish", taskID))
	}
	late, lateOK := lateFinish[taskID]
	if !lateOK {
		panic(fmt.Sprintf("slackForTask: unknown taskID %d in lateFinish", taskID))
	}
	slack := late - early
	if slack < 0 {
		panic(fmt.Sprintf(
			"slackForTask: negative slack (%d) for task %d; lateFinish must never precede earlyFinish for a correctly-computed pass",
			slack, taskID,
		))
	}
	return slack
}

// Exported-shaped entry point: tasks with zero slack form the critical subgraph.
func criticalPathTaskIDs(earlyFinish, lateFinish map[int]int) []int {
	ids := make([]int, 0, len(earlyFinish))
	for taskID := range earlyFinish {
		if slackForTask(taskID, earlyFinish, lateFinish) == 0 {
			ids = append(ids, taskID)
		}
	}
	sort.Ints(ids)
	return ids
}

func demoValidCase() {
	// Correctly-computed forward/backward pass over a 3-task chain A -> B -> C.
	earlyFinish := map[int]int{1: 3, 2: 7, 3: 10}
	lateFinish := map[int]int{1: 3, 2: 7, 3: 10}
	result := criticalPathTaskIDs(earlyFinish, lateFinish)
	fmt.Printf("valid case: criticalPathTaskIDs = %v\n", result)
	if fmt.Sprint(result) != "[1 2 3]" {
		panic("demoValidCase: unexpected result")
	}
}

func demoViolationCase() {
	// Deliberately corrupt lateFinish for task 2 so lateFinish < earlyFinish --
	// simulates a bug in the backward pass that produced an internally inconsistent
	// schedule. The panic in slackForTask exists precisely to catch this class of bug
	// loudly instead of letting criticalPathTaskIDs silently return a
	// wrong-but-plausible result.
	earlyFinish := map[int]int{1: 3, 2: 7, 3: 10}
	lateFinish := map[int]int{1: 3, 2: 5, 3: 10} // bug: lateFinish[2] < earlyFinish[2]
	criticalPathTaskIDs(earlyFinish, lateFinish)
}

func main() {
	demoValidCase()
	fmt.Println("now triggering the contract violation on purpose...")
	demoViolationCase()
	fmt.Println("UNREACHABLE: the panic above should have stopped the program")
}
