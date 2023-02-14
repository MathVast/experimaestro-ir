import logging

from xpmir.distributed import DistributedHook
from xpmir.letor.learner import Learner, ValidationListener
from xpmir.letor.schedulers import LinearWithWarmup
import xpmir.letor.trainers.pairwise as pairwise
from datamaestro import prepare_dataset
from datamaestro_text.transforms.ir import ShuffledTrainingTripletsLines
from datamaestro_text.data.ir import AdhocDocuments, Adhoc
from xpmir.neural.cross import CrossScorer
from experimaestro import experiment, setmeta, RunMode
from experimaestro.launcherfinder import find_launcher
from xpmir.datasets.adapters import RandomFold
from xpmir.evaluation import Evaluations, EvaluationsCollection
from xpmir.interfaces.anserini import AnseriniRetriever, IndexCollection
from xpmir.letor import Device, Random
from xpmir.letor.batchers import PowerAdaptativeBatcher
from xpmir.letor.devices import CudaDevice
from xpmir.letor.optim import (
    AdamW,
    ParameterOptimizer,
    RegexParameterFilter,
    get_optimizers,
    TensorboardService,
)
from xpmir.letor.samplers import TripletBasedSampler
from xpmir.measures import AP, RR, P, nDCG
from xpmir.papers.cli import UploadToHub, paper_command
from xpmir.rankers import collection_based_retrievers, RandomScorer, RetrieverHydrator
from xpmir.rankers.standard import BM25
from xpmir.text.huggingface import DualTransformerEncoder
from xpmir.utils.utils import find_java_home
from .configuration import Monobert

logging.basicConfig(level=logging.INFO)


@paper_command(schema=Monobert, package=__package__)
def cli(xp: experiment, cfg: Monobert, upload_to_hub: UploadToHub, run_mode: RunMode):
    """monoBERT trained on MS-Marco

    Passage Re-ranking with BERT (Rodrigo Nogueira, Kyunghyun Cho). 2019.
    https://arxiv.org/abs/1901.04085
    """

    # Define the different launchers
    launcher_index = find_launcher(cfg.indexation.requirements)
    launcher_learner = find_launcher(cfg.learner.requirements)
    launcher_evaluate = find_launcher(cfg.evaluation.requirements)

    # Sets the working directory and the name of the xp
    # Needed by Pyserini
    xp.setenv("JAVA_HOME", find_java_home())

    # Misc
    device = CudaDevice() if cfg.gpu else Device()
    random = Random(seed=0)
    basemodel = BM25().tag("model", "bm25")

    # create a random scorer as the most naive baseline
    random_scorer = RandomScorer(random=random).tag("reranker", "random")
    measures = [AP, P @ 20, nDCG, nDCG @ 10, nDCG @ 20, RR, RR @ 10]

    # Creates the directory with tensorboard data
    tb = xp.add_service(TensorboardService(xp.resultspath / "runs"))

    # Datasets: train, validation and test
    documents: AdhocDocuments = prepare_dataset("irds.msmarco-passage.documents")
    devsmall: Adhoc = prepare_dataset("irds.msmarco-passage.dev.small")
    dev: Adhoc = prepare_dataset("irds.msmarco-passage.dev")

    # Sample the dev set to create a validation set
    ds_val = RandomFold(
        dataset=dev,
        seed=123,
        fold=0,
        sizes=[cfg.learner.validation_size],
        exclude=devsmall.topics,
    ).submit()

    # Prepares the test collections evaluation
    tests = EvaluationsCollection(
        msmarco_dev=Evaluations(devsmall, measures),
        trec2019=Evaluations(
            prepare_dataset("irds.msmarco-passage.trec-dl-2019"), measures
        ),
        trec2020=Evaluations(
            prepare_dataset("irds.msmarco-passage.trec-dl-2020"), measures
        ),
    )

    # Setup indices and validation/test base retrievers
    @collection_based_retrievers
    def retrievers(documents: AdhocDocuments):
        index = IndexCollection(documents=documents).submit(launcher=launcher_index)
        return lambda *, k: RetrieverHydrator(
            store=documents,
            retriever=AnseriniRetriever(index=index, k=k, model=basemodel),
        )

    @collection_based_retrievers
    def model_based_retrievers(documents: AdhocDocuments):
        def factory(*, base_factory, model, device=None):
            base_retriever = base_factory(documents)
            return model.getRetriever(
                base_retriever,
                cfg.retrieval.batch_size,
                PowerAdaptativeBatcher(),
                device=device,
            )

        return factory

    val_retrievers = retrievers.factory(k=cfg.retrieval.val_k)
    test_retrievers = retrievers.factory(k=cfg.retrieval.k)

    # Search and evaluate with the base model
    tests.evaluate_retriever(test_retrievers, launcher_index)

    # Search and evaluate with a random reranker
    tests.evaluate_retriever(
        model_based_retrievers.factory(
            base_factory=test_retrievers, model=random_scorer
        )
    )

    # Defines how we sample train examples
    # (using the shuffled pre-computed triplets from MS Marco)
    train_triples = prepare_dataset("irds.msmarco-passage.train.docpairs")
    triplesid = ShuffledTrainingTripletsLines(
        seed=123,
        data=train_triples,
    ).submit()
    train_sampler = TripletBasedSampler(source=triplesid, index=documents)

    # define the trainer for monobert
    monobert_trainer = pairwise.PairwiseTrainer(
        lossfn=pairwise.PointwiseCrossEntropyLoss(),
        sampler=train_sampler,
        batcher=PowerAdaptativeBatcher(),
        batch_size=cfg.learner.batch_size,
    )

    monobert_scorer: CrossScorer = CrossScorer(
        encoder=DualTransformerEncoder(
            model_id="bert-base-uncased", trainable=True, maxlen=512, dropout=0.1
        )
    ).tag("reranker", "monobert")

    # The validation listener evaluates the full retriever
    # (retriever + reranker) and keep the best performing model
    # on the validation set
    validation = ValidationListener(
        dataset=ds_val,
        retriever=model_based_retrievers.factory(
            base_factory=val_retrievers, model=monobert_scorer, device=device
        )(documents),
        validation_interval=cfg.learner.validation_interval,
        metrics={"RR@10": True, "AP": False, "nDCG": False},
    )

    # Setup the parameter optimizers
    scheduler = LinearWithWarmup(
        num_warmup_steps=cfg.learner.num_warmup_steps,
        min_factor=cfg.learner.warmup_min_factor,
    )

    optimizers = [
        ParameterOptimizer(
            scheduler=scheduler,
            optimizer=AdamW(lr=cfg.learner.lr, eps=1e-6),
            filter=RegexParameterFilter(includes=[r"\.bias$", r"\.LayerNorm\."]),
        ),
        ParameterOptimizer(
            scheduler=scheduler,
            optimizer=AdamW(lr=cfg.learner.lr, weight_decay=1e-2, eps=1e-6),
        ),
    ]

    # The learner trains the model
    learner = Learner(
        # Misc settings
        device=device,
        random=random,
        # How to train the model
        trainer=monobert_trainer,
        # The model to train
        scorer=monobert_scorer,
        # Optimization settings
        steps_per_epoch=cfg.learner.steps_per_epoch,
        optimizers=get_optimizers(optimizers),
        max_epochs=cfg.learner.max_epochs,
        # The listeners (here, for validation)
        listeners={"bestval": validation},
        # The hook used for evaluation
        hooks=[setmeta(DistributedHook(models=[monobert_scorer]), True)],
    )

    # Submit job and link
    outputs = learner.submit(launcher=launcher_learner)
    tb.add(learner, learner.logpath)

    # Evaluate the neural model on test collections
    for metric_name in validation.monitored():
        model = outputs.listeners["bestval"][metric_name]  # type: CrossScorer
        tests.evaluate_retriever(
            model_based_retrievers.factory(
                model=model, base_factory=test_retrievers, device=device
            ),
            launcher_evaluate,
            model_id=f"monobert-{metric_name}",
        )

    # Waits that experiments complete
    xp.wait()

    if run_mode == RunMode.NORMAL:
        # Upload to HUB if requested
        upload_to_hub.send_scorer(
            {"monobert-RR@10": outputs.listeners["bestval"]["RR@10"]}, evaluations=tests
        )

        # Display metrics for each trained model
        tests.output_results()


if __name__ == "__main__":
    cli()
