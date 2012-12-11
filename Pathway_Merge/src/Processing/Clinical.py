'''
Created on Oct 9, 2012

@author: agross
'''
import rpy2.robjects as robjects
import pandas.rpy.common as com 


from numpy import nan, sort, log
from scipy.stats import f_oneway, fisher_exact, pearsonr
from pandas import Series, DataFrame, notnull, crosstab, read_csv

from Helpers import bhCorrection, extract_pc, match_series, drop_first_norm_pc
from Helpers import cluster_down, df_to_binary_vec, run_rate_permutation
from Data.Firehose import get_mutation_matrix, get_cna_matrix
from Data.Firehose import read_rnaSeq, read_methylation, read_mrna
from Data.Pathways import build_meta_matrix
from Data.AgingData import get_age_signal


survival = robjects.packages.importr('survival')
base = robjects.packages.importr('base')
MIN_NUM_HITS = 8
robjects.r.options(warn=-1);
zz = robjects.r.file("all.Rout", open="wt")
robjects.r.sink(zz, type='message')

GENE_LENGTHS = read_csv('/cellar/users/agross/Data/GeneSets/coding_lengths.csv', 
                        index_col=0, squeeze=True)

def delambda(f):
    def f_(a): return f(a)
    return f_

def anova(hit_vec, response_vec):
    '''
    Wrapper to do a one way anova on pandas Series
    ------------------------------------------------
    hit_vec: Series of labels
    response_vec: Series of measurements
    '''
    hit_vec, response_vec = match_series(hit_vec, response_vec)
    return f_oneway(*[response_vec[hit_vec == num] for num in 
                      hit_vec.unique()])[1]

def fisher_exact_test(hit_vec, response_vec):
    '''
    Wrapper to do a fischer's exact test on pandas Series
    ------------------------------------------------
    hit_vec: Series of labels (boolean, or (0,1))
    response_vec: Series of measurements (boolean, or (0,1))
    '''
    #assert ((len(hit_vec.unique()) <= 2) and 
    #        (len(response_vec.unique()) <= 2)) 
    cont_table = crosstab(hit_vec, response_vec)
    if (cont_table.shape != (2,2)):
        return 1
    return fisher_exact(cont_table)[1]

def pearson_p(a,b):
    '''
    Find pearson's correlation and return p-value.
    ------------------------------------------------
    a, b: Series with continuous measurements
    '''
    a,b = match_series(a.dropna(), b.dropna())
    _,p = pearsonr(a,b)
    return p

def get_cox_ph(clinical, hit_vec, covariates=[], time_var='days',
               event_var='censored', return_val='p'):
    '''
    Fit a cox proportial hazzards model to the data.
    Returns a p-value on the hit_vec coefficient. 
    ---------------------------------------------------
    clinical: DataFrame of clinical variables
    hit_vec: vector of labels to test against
    covariates: names of covariates in the cox model,
                (must be columns in clinical DataFrame)
    '''
    if not all([cov in clinical for cov in covariates]):
        missing = [cov for cov in covariates if cov not in clinical]
        covariates = [cov for cov in covariates if cov in clinical]
        print ', '.join(missing) + ' not in clinical data... running anyway.'
    hit_vec.name = 'pathway'
    factors = ['pathway'] + covariates
    df = clinical.join(hit_vec)
    df = df[factors + [time_var, event_var]]
    #df = df.ix[patients]
    df[factors] = (df[factors] - df[factors].mean())
    df = com.convert_to_r_dataframe(df) #@UndefinedVariable
    #fmla = 'Surv(' + time_var + ', ' + event_var + ') ~ '+ '+'.join(factors)
    interactions = '+'.join(['pathway*' + c for c in covariates])
    if len(covariates) == 0:
        interactions = 'pathway'
    fmla = 'Surv(' + time_var + ', ' + event_var + ') ~ ' + interactions
    fmla = robjects.Formula(fmla)
    try:
        s = survival.coxph(fmla, df)
        results = com.convert_robj(dict(base.summary(s).iteritems())['coefficients'])
    except robjects.rinterface.RRuntimeError:
        return 1.23
    if return_val == 'p':
        return results.ix['pathway','Pr(>|z|)']
    else:
        print dict(base.summary(s).iteritems())['logtest']
        return results
    
def get_tests(clinical, survival_tests, real_variables, binary_variables,
              var_type='boolean'):
    '''
    Gets clinical association tests for a boolean or real valued response 
    variable.
    Does some quick checks to see if the data is sufficent for each test. 
    Note that clinical values are now hard coded into each test function.
    ------------------------------------------------------------------------
    clinical: DataFrame of clinical variables
    covariates: names of covariates to be passed to the cox model,
                (must be columns in clinical DataFrame)
    '''
    check_surv = lambda s: ((s['event_var'] in clinical) and 
                 (len(clinical[[s['event_var'], s['time_var']]].dropna()) > 2))
    make_surv = lambda args: lambda vec: get_cox_ph(clinical, vec, **args)
    surv_tests = dict((test, make_surv(args)) for test,args in 
                      survival_tests.iteritems() if check_surv(args))
    
    check_test = lambda t: (t in clinical) and (notnull(clinical[t]).sum() > 10)
    make_anova = lambda test: lambda vec: anova(vec, clinical[test])
    make_anova_r = lambda test: lambda vec: anova(clinical[test], vec)
    make_fisher = lambda test: lambda vec: fisher_exact_test(vec, clinical[test])
    make_pcc = lambda test: lambda vec: pearson_p(vec, clinical[test])
    
    if var_type == 'boolean':
        real_test, bin_test = make_anova, make_fisher
    elif var_type == 'real':
        real_test, bin_test = make_pcc, make_anova_r
            
    real_tests = dict((test, real_test(test)) for test in real_variables
                      if check_test(test))    
    bin_tests = dict((test, bin_test(test)) for test in binary_variables
                      if check_test(test))
    
    return dict(list(surv_tests.items()) + list(real_tests.items()) + 
                list(bin_tests.items()))

def run_tests(tests, data_frame):
    '''
    Runs each test in tests across the rows of the DataFrame.
    Returns the p-values, and the corrected q-values.
    ------------------------------------------------------------
    tests: dictionary mapping test name to test functions
    data_frame: DataFrame of response variables to test against
    '''
    p_values = DataFrame()
    for test, f in tests.iteritems():
        p_vec = Series(dict((p, f(vec)) for p, vec in data_frame.iterrows()), 
                       name=test)
        p_values = p_values.join(p_vec, how='outer')
    q_values = p_values.apply(bhCorrection)
    return p_values, q_values

def run_clinical_bool(cancer, clinical, data_path, gene_sets, 
                      survival_tests, real_variables, binary_variables,
                      data_type='mutation'):
    '''
    Runs clinical tests for boolean type data (mutation, amplification, 
    or deletion).
    '''
    if data_type == 'mutation':
        hit_matrix, _ = get_mutation_matrix(cancer, data_path)
    elif data_type in ['amplification', 'deletion']:
        hit_matrix, lesion_matrix = get_cna_matrix(data_path, data_type)
    
    single_matrix = lesion_matrix if data_type == 'amplification' else hit_matrix
    single_matrix = single_matrix.clip_upper(1.)
    clinical['rate'] = log(single_matrix.sum(0))
    tests = get_tests(clinical, survival_tests, real_variables, binary_variables,
                      var_type='boolean')
    gene_counts = sort(single_matrix.sum(1))
    good_genes = gene_counts[gene_counts > MIN_NUM_HITS].index[-500:]
    p_genes, q_genes = run_tests(tests, single_matrix.ix[good_genes])
    
    clinical['rate'] = log(hit_matrix.sum(0))
    meta_matrix = build_meta_matrix(gene_sets, hit_matrix, 
                                    set_filter=lambda s: s.clip_upper(1))
    tests = get_tests(clinical, survival_tests, real_variables, binary_variables,
                      var_type='boolean')
    p_pathways, q_pathways = run_tests(tests, meta_matrix.clip_upper(1.))
    
    lengths = GENE_LENGTHS.ix[hit_matrix.index].dropna()
    _res = run_rate_permutation(meta_matrix, hit_matrix, gene_sets, 
                               lengths, tests['rate'])
    q_pathways['rate']  = _res 
    return locals()

def run_clinical_real(cancer, clinical, stddata_path, gene_sets,
                      survival_tests, real_variables, binary_variables,
                      data_type='expression', drop_pc=False):
    
    if data_type == 'expression':
        data_matrix = read_rnaSeq(stddata_path)
        data_matrix = data_matrix.groupby(by=lambda n: n.split('|')[0]).mean()
    elif data_type == 'expression_array':
        data_matrix = read_mrna(stddata_path)
    elif data_type == 'methylation':
        data_matrix = read_methylation(stddata_path)
    if drop_pc:
        data_matrix = drop_first_norm_pc(data_matrix)
    pc = dict((p, extract_pc(data_matrix.ix[g])) for p, g in 
              gene_sets.iteritems())
    pc = DataFrame(dict((p, (v - v.mean()) / v.std()) for p,v in pc.iteritems() if 
                   type(v) != type(None))).T
    #clinical['pc'] = extract_pc(data_matrix.dropna(), pc_threshold=0)
    tests  = get_tests(clinical, survival_tests, real_variables, 
                       binary_variables, var_type='real')
    #return locals()
    p_pathways, q_pathways = run_tests(tests, pc)
    return locals()


class ClinicalObject(object):
    '''
    Wrapper to store the data in a nice object rather than have to unpack it. 
    '''
    def __init__(self, cancer, data_path, gene_sets, data_type='mutation',
                 drop_pc=False, survival_tests={}, real_variables=[],
                 binary_variables=[]):
        stddata_path = data_path.replace('analyses', 'stddata')
        clinical = read_csv(stddata_path + 'Clinical/compiled.csv', index_col=0)
        if hasattr(clinical, 'event'):
            clinical['event'] = clinical[['event','deceased']].sum(1).clip_upper(1.)
        try:
            meth_age, amar = get_age_signal(stddata_path, clinical)
            clinical['meth_age'] = meth_age
            clinical['AMAR'] = amar
        except:
            pass  #Probably because there is not a 450k chip for the cancer
    
        if data_type in ['mutation', 'amplification','deletion']:
            o_dict = run_clinical_bool(cancer, clinical, data_path, gene_sets, 
                                       survival_tests, real_variables, 
                                       binary_variables, data_type)
        else:
            o_dict = run_clinical_real(cancer, clinical, stddata_path, 
                                       gene_sets, survival_tests, real_variables, 
                                       binary_variables, data_type, drop_pc)
        self.__dict__ = o_dict
        self.stddata_path = stddata_path
        self.data_path = data_path
        #self.patients = clinical[['days','censored']].dropna().index
        
        
    def filter_bad_pathways(self, gene_lookup):
        for clin_type, p_vals in self.p_genes.iteritems():
            for gene in set(p_vals.index).intersection(set(gene_lookup)):
                for pathway in gene_lookup[gene]:
                    if ((pathway in self.q_pathways[clin_type]) and 
                       (p_vals[gene] < self.p_pathways[clin_type][pathway]) and
                        (self.hit_matrix.ix[gene].sum() > 
                         self.meta_matrix.ix[pathway].sum()*.5)):
                        self.p_pathways[clin_type][pathway] = nan
            self.q_pathways[clin_type] = bhCorrection(self.p_pathways[clin_type])
            
    def cluster_down(self, num_clusters=50, draw_dendrogram=False):
        if self.data_type in ['mutation', 'amplification','deletion']:
            matrix = (self.meta_matrix > 0).astype(int)
            agg_function = df_to_binary_vec
            dist_metric = 'jaccard'
        else:
            matrix = self.pc
            agg_function = extract_pc
            dist_metric = 'euclidean'
        r = cluster_down(matrix, agg_function, dist_metric, num_clusters,
                         draw_dendrogram)
        self.clustered, self.assignments = r[0], r[1]
        if draw_dendrogram:
            return r[2]
        
        