from typing import List
from datamaestro_text.data.ir import Adhoc
from experimaestro import param, task, pathoption, config
import xpmir as ir
from datamaestro_text.data.ir.trec import (
    TrecAdhocAssessments,
    TrecAdhocRun,
    TrecAdhocResults,
)

import logging
import xpmir.metrics as metrics
from xpmir.rankers import Retriever


@param("assessments", TrecAdhocAssessments)
@param("run", TrecAdhocRun)
@param("metrics", type=List[str], default=["map", "p@20", "ndcg", "ndcg@20", "mrr"])
@pathoption("aggregated", "aggregated.dat")
@pathoption("detailed", "detailed.dat")
@task()
class TrecEval:
    def config(self):
        return TrecAdhocResults(
            results=self.aggregated, detailed=self.detailed, metrics=self.metrics
        )

    def execute(self):
        """Evaluate an IR ad-hoc run with trec-eval"""

        detailed = metrics.calc(
            str(self.assessments.path), str(self.run.path), self.metrics
        )
        means = metrics.mean(detailed)
        print(means)

        def print_line(fp, measure, scope, value):
            fp.write("{:25s}{:8s}{:.4f}\n".format(measure, scope, value))

        with self.detailed.open("w") as fp:
            for measure, values in detailed.items():
                for query_id, value in sorted(values.items()):
                    print_line(fp, measure, query_id, value)

        # Scope hack: use query_measures of last item in previous loop to
        # figure out all unique measure names.
        #
        # TODO(cvangysel): add member to RelevanceEvaluator
        #                  with a list of measure names.
        with self.aggregated.open("w") as fp:
            for measure, value in sorted(means.items()):
                print_line(fp, measure, "all", value)


@param("dataset", type=Adhoc)
@param("retriever", type=Retriever)
@param("metrics", type=List[str], default=["map", "p@20", "ndcg", "ndcg@20", "mrr"])
@pathoption("detailed", "detailed.txt")
@pathoption("measures", "measures.txt")
@pathoption("run_path", "retrieved.trecrun")
@task()
class Evaluate:
    def config(self):
        return TrecAdhocResults(
            results=self.measures, detailed=self.detailed, metrics=self.metrics
        )

    def execute(self):

        # retriever.initialize(random)
        # retriever.load(state_path)
        # self.dataset.initialize(retriever.vocab)
        # self.retriever.initialize(self.retriever_path, self.metrics, random, retriever, self.dataset)

        # with self.logger.duration('testing'):
        #     test_ctxt = self.retriever.run({
        #         "epoch": data["valid_epoch"],
        #         "retriever": lambda: retriever
        #     })

        # Run the model
        self.retriever.initialize()
        with self.run_path.open("w") as fp:
            for qid, query in self.dataset.topics.iter():
                for rank, sd in enumerate(self.retriever.retrieve(query)):
                    fp.write(f"""{qid} Q0 {sd.docid} {rank+1} {sd.score} run\n""")

        qrels_path = str(self.dataset.assessments.trecpath())
        calculated_metrics = metrics.calc(qrels_path, str(self.run_path), self.metrics)
        metrics_by_query = calculated_metrics
        mean_metrics = metrics.mean(calculated_metrics)

        def print_line(fp, measure, scope, value):
            fp.write("{:25s}{:8s}{:.4f}\n".format(measure, scope, value))

        with open(self.measures, "wt") as fp:
            for measure, value in sorted(mean_metrics.items()):
                print_line(fp, measure, "all", value)

        with open(self.detailed, "wt") as fp:
            for measure, value in sorted(metrics_by_query.items()):
                for query, value in value.items():
                    print_line(fp, measure, query, value)
